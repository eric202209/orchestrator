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
from app.celery_app import celery_app
from app.models import (
    Session as SessionModel,
    SessionTask,
    Task,
    TaskStatus,
    LogEntry,
    Project,
)
from app.database import get_db_session
from app.services import OpenClawSessionService, PromptTemplates
from app.services.error_handler import error_handler
from app.services.checkpoint_service import CheckpointService
from app.services.project_isolation_service import resolve_project_workspace_path
from app.services.task_service import TaskService
from app.services.prompt_templates import (
    OrchestrationStatus,
    OrchestrationState,
    StepResult,
)

logger = logging.getLogger(__name__)


class TaskWorkspaceViolationError(ValueError):
    """Raised when a planned command escapes the task workspace."""

    pass


def _set_session_alert(
    session: Optional[SessionModel],
    level: Optional[str] = None,
    message: Optional[str] = None,
) -> None:
    if not session:
        return
    session.last_alert_level = level
    session.last_alert_message = message
    session.last_alert_at = datetime.utcnow() if message else None


def _get_next_pending_project_task(
    db: Session, project_id: Optional[int]
) -> Optional[Task]:
    if not project_id:
        return None
    return TaskService(db).get_next_pending_task(project_id)


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
            "project_dir_override": (
                str(orchestration_state.project_dir)
                if orchestration_state._project_dir_override
                else None
            ),
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


TOOL_FAILURE_PATTERNS = (
    "read failed: ENOENT",
    "read failed: EISDIR",
    "exec failed: exec preflight",
    "complex interpreter invocation detected",
    "no such file or directory, access",
    "illegal operation on a directory, read",
)


def _recent_step_tool_failures(
    db: Session,
    session_id: int,
    task_id: int,
    started_at: datetime,
) -> List[str]:
    recent_logs = (
        db.query(LogEntry)
        .filter(
            LogEntry.session_id == session_id,
            LogEntry.task_id == task_id,
            LogEntry.created_at >= started_at,
        )
        .order_by(LogEntry.created_at.asc(), LogEntry.id.asc())
        .all()
    )
    matches: List[str] = []
    for log in recent_logs:
        message = str(log.message or "")
        lowered = message.lower()
        if any(pattern.lower() in lowered for pattern in TOOL_FAILURE_PATTERNS):
            matches.append(message[:500])
    return matches


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
        "structured_state": {
            "task_id": task.id,
            "project_id": task.project_id,
            "title": task.title,
            "status": task.status.value,
            "plan_position": getattr(task, "plan_position", None),
            "execution_profile": getattr(task, "execution_profile", None),
            "workspace_status": getattr(task, "workspace_status", None),
            "task_subfolder": getattr(task, "task_subfolder", None),
            "created_at": task.created_at.isoformat(),
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "completed_at": (
                task.completed_at.isoformat() if task.completed_at else None
            ),
            "error_message": task.error_message,
        },
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
        structured_state = report.get("structured_state", {})
        if structured_state:
            report_text += "## Structured State\n\n"
            report_text += "```json\n"
            report_text += json.dumps(structured_state, indent=2)
            report_text += "\n```\n\n"
        report_text += "## Logs\n\n"
        for log in report["logs"]:
            report_text += f"- [{log['level']}] {log['message']}\n"

        return {"report": report_text, "format": "markdown"}

    return {"report": report, "format": output_format}


def _is_verification_style_task(
    execution_profile: str, title: Optional[str], description: Optional[str]
) -> bool:
    combined = f"{execution_profile} {title or ''} {description or ''}".lower()
    markers = (
        "verify",
        "verification",
        "refine",
        "review",
        "qa",
        "audit",
        "integration",
        "test",
    )
    return execution_profile in {"test_only", "review_only"} or any(
        marker in combined for marker in markers
    )


def _get_task_report_path(project_root: Path, task: Task) -> Optional[Path]:
    if not task or not getattr(task, "task_subfolder", None):
        return None
    report_path = project_root / task.task_subfolder / f"task_report_{task.id}.md"
    return report_path


def _get_state_manager_path(project_root: Path) -> Path:
    return project_root / ".openclaw" / "state_manager.json"


def _build_project_state_snapshot(
    db: Session,
    project: Optional[Project],
    current_task: Optional[Task],
    session_id: Optional[int],
) -> Dict[str, Any]:
    if not project:
        return {
            "project_id": None,
            "project_name": None,
            "session_id": session_id,
            "status": "unknown",
            "updated_at": datetime.utcnow().isoformat(),
            "tasks": [],
        }

    task_service = TaskService(db)
    ordered_tasks = task_service.get_project_tasks(project.id)
    inconsistent_pairs = []
    highest_incomplete_position = None
    for task in ordered_tasks:
        if task.plan_position is None:
            continue
        if task.status != TaskStatus.DONE:
            highest_incomplete_position = task.plan_position
            break

    if highest_incomplete_position is not None:
        for task in ordered_tasks:
            if (
                task.plan_position is not None
                and task.plan_position > highest_incomplete_position
                and task.status == TaskStatus.DONE
            ):
                inconsistent_pairs.append(
                    {
                        "task_id": task.id,
                        "plan_position": task.plan_position,
                        "title": task.title,
                    }
                )

    failed_or_cancelled = [
        task
        for task in ordered_tasks
        if task.status in {TaskStatus.FAILED, TaskStatus.CANCELLED}
    ]
    overall_status = "ready"
    if failed_or_cancelled or inconsistent_pairs:
        overall_status = "unsynced"
    elif any(task.status == TaskStatus.RUNNING for task in ordered_tasks):
        overall_status = "running"
    elif any(task.status == TaskStatus.PENDING for task in ordered_tasks):
        overall_status = "pending"

    return {
        "project_id": project.id,
        "project_name": project.name,
        "session_id": session_id,
        "current_task_id": current_task.id if current_task else None,
        "current_task_title": current_task.title if current_task else None,
        "status": overall_status,
        "updated_at": datetime.utcnow().isoformat(),
        "failed_or_cancelled_task_ids": [task.id for task in failed_or_cancelled],
        "inconsistent_completed_tasks": inconsistent_pairs,
        "tasks": [
            {
                "task_id": task.id,
                "title": task.title,
                "plan_position": task.plan_position,
                "status": task.status.value,
                "workspace_status": getattr(task, "workspace_status", None),
                "task_subfolder": getattr(task, "task_subfolder", None),
            }
            for task in ordered_tasks
        ],
    }


def _write_project_state_snapshot(
    db: Session,
    project: Optional[Project],
    current_task: Optional[Task],
    session_id: Optional[int],
) -> None:
    if not project:
        return
    project_root = resolve_project_workspace_path(project.workspace_path, project.name)
    state_path = _get_state_manager_path(project_root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _build_project_state_snapshot(db, project, current_task, session_id)
    state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _workspace_snapshot_key(task_id: int) -> str:
    return f"task-{task_id}-pre-run"


def _snapshot_workspace_before_run(
    task_service: TaskService,
    project: Optional[Project],
    task_id: int,
    target_dir: Path,
    *,
    preserve_project_root_rules: bool,
) -> Optional[Dict[str, Any]]:
    if not project:
        return None
    return task_service.create_workspace_snapshot(
        project,
        target_dir,
        snapshot_key=_workspace_snapshot_key(task_id),
        preserve_project_root_rules=preserve_project_root_rules,
    )


def _restore_workspace_after_abort(
    task_service: TaskService,
    project: Optional[Project],
    task_id: int,
    target_dir: Path,
    *,
    preserve_project_root_rules: bool,
) -> Optional[Dict[str, Any]]:
    if not project:
        return None
    return task_service.restore_workspace_snapshot(
        project,
        target_dir,
        snapshot_key=_workspace_snapshot_key(task_id),
        preserve_project_root_rules=preserve_project_root_rules,
    )


def _run_virtual_merge_gate(
    db: Session,
    project: Optional[Project],
    current_task: Optional[Task],
    execution_profile: str,
) -> Optional[str]:
    if not project or not current_task:
        return None
    if not _is_verification_style_task(
        execution_profile, current_task.title, current_task.description
    ):
        return None
    if current_task.plan_position is None:
        return None

    project_root = resolve_project_workspace_path(project.workspace_path, project.name)
    prior_tasks = [
        task
        for task in TaskService(db).get_project_tasks(project.id)
        if task.id != current_task.id
        and task.plan_position is not None
        and task.plan_position < current_task.plan_position
    ]
    incomplete = [task for task in prior_tasks if task.status != TaskStatus.DONE]
    if incomplete:
        summary = ", ".join(
            f"#{task.plan_position} {task.title} ({task.status.value})"
            for task in incomplete[:3]
        )
        return f"Virtual merge gate failed: earlier ordered tasks are incomplete: {summary}"

    missing_reports = []
    for task in prior_tasks:
        report_path = _get_task_report_path(project_root, task)
        if report_path and not report_path.exists():
            missing_reports.append(
                f"#{task.plan_position} {task.title} (missing {report_path.name})"
            )
    if missing_reports:
        return (
            "Virtual merge gate failed: missing structured task reports for prior work: "
            + ", ".join(missing_reports[:3])
        )

    state_path = _get_state_manager_path(project_root)
    if state_path.exists():
        try:
            state_data = json.loads(state_path.read_text(encoding="utf-8"))
            if state_data.get("status") == "unsynced":
                return (
                    "Virtual merge gate failed: project state manager is UNSYNCED. "
                    "Resolve earlier task inconsistencies before verify/refine."
                )
        except Exception:
            return "Virtual merge gate failed: state manager file is unreadable"

    return None


def _step_needs_command_repair(step: Dict[str, Any]) -> bool:
    commands = step.get("commands", [])
    if not isinstance(commands, list):
        return True
    return not any(str(command or "").strip() for command in commands)


def _build_step_repair_prompt(
    task_prompt: str,
    step: Dict[str, Any],
    step_index: int,
    project_dir: Path,
    prior_results_summary: str,
    project_context: str,
) -> str:
    return f"""Repair this execution step so it becomes machine-runnable JSON. Return JSON object only.

Task:
{task_prompt[:2000]}

Current step index:
{step_index + 1}

Current step JSON:
{json.dumps(step, indent=2)[:4000]}

Project context:
{project_context[:3000]}

Prior completed results:
{prior_results_summary[:2000]}

Rules:
1. Working directory is {project_dir}
2. Use relative paths only
3. Do not use .., ~, or absolute paths
4. commands must be a non-empty JSON array of shell commands
5. verification and rollback may be null
6. expected_files must be a JSON array
7. Keep the step intent the same
8. Output JSON object only, no prose

Example:
{{
  "step_number": 1,
  "description": "Inspect project structure and locate implementation entry points",
  "commands": ["rg --files . | head -100"],
  "verification": "test -d . && echo ok",
  "rollback": null,
  "expected_files": []
}}
"""


def _looks_like_truncated_multistep_plan(
    output_text: str, extracted_plan: Optional[List[Dict[str, Any]]]
) -> bool:
    """Detect mixed-content planning output that collapsed into a single-step plan."""
    if not extracted_plan or len(extracted_plan) != 1:
        return False

    text = output_text or ""
    step_number_mentions = len(
        re.findall(
            r'(?:\\)?["\']step_number(?:\\)?["\']\s*:\s*\d+', text, flags=re.IGNORECASE
        )
    )
    if step_number_mentions > 1:
        return True

    if re.search(
        r'(?:\\)?["\']step_number(?:\\)?["\']\s*:\s*[2-9]\d*',
        text,
        flags=re.IGNORECASE,
    ):
        return True

    description_mentions = len(
        re.findall(
            r'(?:\\)?["\']description(?:\\)?["\']\s*:', text, flags=re.IGNORECASE
        )
    )
    if description_mentions > 1:
        return True

    return False


def _repair_step_commands_with_self_correction(
    openclaw_service: Any,
    db: Session,
    session_id: int,
    task_id: int,
    session_instance_id: Optional[str],
    task_prompt: str,
    step: Dict[str, Any],
    step_index: int,
    project_dir: Path,
    prior_results_summary: str,
    project_context: str,
    logger_obj: logging.Logger,
) -> Optional[Dict[str, Any]]:
    repair_prompt = _build_step_repair_prompt(
        task_prompt=task_prompt,
        step=step,
        step_index=step_index,
        project_dir=project_dir,
        prior_results_summary=prior_results_summary,
        project_context=project_context,
    )
    repair_result = asyncio.run(
        openclaw_service.execute_task(repair_prompt, timeout_seconds=120)
    )
    repair_output = _extract_structured_text(repair_result.get("output", "{}"))
    success, repair_data, strategy_info = error_handler.attempt_json_parsing(
        repair_output, context="step_repair"
    )
    if not success or not isinstance(repair_data, dict):
        logger_obj.warning(
            "[ORCHESTRATION] Step %s self-correction failed to parse: %s",
            step_index + 1,
            strategy_info,
        )
        _record_live_log(
            db,
            session_id,
            task_id,
            "WARN",
            f"[ORCHESTRATION] Step {step_index + 1} self-correction failed: {strategy_info}",
            session_instance_id=session_instance_id,
            metadata={"phase": "step_validation", "strategy": strategy_info},
        )
        return None

    repaired_step = _normalize_step(
        repair_data, project_dir, logger_obj, step_index + 1
    )
    if _step_needs_command_repair(repaired_step):
        _record_live_log(
            db,
            session_id,
            task_id,
            "WARN",
            (
                f"[ORCHESTRATION] Step {step_index + 1} self-correction returned no runnable commands"
            ),
            session_instance_id=session_instance_id,
            metadata={"phase": "step_validation"},
        )
        return None

    _record_live_log(
        db,
        session_id,
        task_id,
        "INFO",
        f"[ORCHESTRATION] Step {step_index + 1} repaired by self-correction",
        session_instance_id=session_instance_id,
        metadata={"phase": "step_validation", "strategy": strategy_info},
    )
    return repaired_step


def _extract_plan_steps(parsed_planning_output: Any) -> Optional[List[Dict[str, Any]]]:
    """Accept common planning response wrappers and return the step list."""

    def looks_like_single_step(candidate: Any) -> bool:
        if not isinstance(candidate, dict):
            return False

        step_like_keys = {
            "step_number",
            "description",
            "commands",
            "verification",
            "rollback",
            "expected_files",
        }
        return bool(step_like_keys.intersection(candidate.keys()))

    def looks_like_plan_steps(candidate: Any) -> bool:
        if not isinstance(candidate, list) or not candidate:
            return False

        required_hint_keys = {
            "step_number",
            "description",
            "commands",
            "verification",
            "rollback",
            "expected_files",
        }
        saw_step_like_item = False

        for item in candidate:
            if not isinstance(item, dict):
                return False
            if required_hint_keys.intersection(item.keys()):
                saw_step_like_item = True

        return saw_step_like_item

    if looks_like_single_step(parsed_planning_output):
        return [parsed_planning_output]

    if looks_like_plan_steps(parsed_planning_output):
        return parsed_planning_output

    if isinstance(parsed_planning_output, list):
        for item in parsed_planning_output:
            nested_plan = _extract_plan_steps(item)
            if nested_plan is not None:
                return nested_plan
        return None

    if not isinstance(parsed_planning_output, dict):
        return None

    priority_keys = (
        "steps",
        "plan",
        "task_plan",
        "execution_plan",
        "revised_plan",
        "remaining_steps",
        "workflow",
        "items",
    )
    for key in priority_keys:
        candidate = parsed_planning_output.get(key)
        if looks_like_single_step(candidate):
            return [candidate]
        if looks_like_plan_steps(candidate):
            return candidate

    payloads = parsed_planning_output.get("payloads")
    if isinstance(payloads, list):
        for payload in payloads:
            nested_plan = _extract_plan_steps(payload)
            if nested_plan is not None:
                return nested_plan

    for value in parsed_planning_output.values():
        if looks_like_single_step(value):
            return [value]
        if looks_like_plan_steps(value):
            return value

    for value in parsed_planning_output.values():
        if isinstance(value, (dict, list)):
            nested_plan = _extract_plan_steps(value)
            if nested_plan is not None:
                return nested_plan

    return None


def _extract_structured_text(value: Any) -> str:
    """Recover human/model text from common OpenClaw payload shapes."""

    if value is None:
        return ""

    if isinstance(value, str):
        return value

    if isinstance(value, list):
        parts = [_extract_structured_text(item) for item in value]
        return "\n".join(part for part in parts if part)

    if not isinstance(value, dict):
        return str(value)

    for key in ("text", "output_text", "content_text"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate

    content = value.get("content")
    if isinstance(content, list):
        content_text = _extract_structured_text(content)
        if content_text.strip():
            return content_text
    elif isinstance(content, str) and content.strip():
        return content

    message = value.get("message")
    if isinstance(message, (dict, list, str)):
        message_text = _extract_structured_text(message)
        if message_text.strip():
            return message_text

    payloads = value.get("payloads")
    if isinstance(payloads, list):
        payload_text = _extract_structured_text(payloads)
        if payload_text.strip():
            return payload_text

    for candidate in value.values():
        if isinstance(candidate, (dict, list, str)):
            nested_text = _extract_structured_text(candidate)
            if nested_text.strip():
                return nested_text

    return json.dumps(value)


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


def _missing_expected_files(
    project_dir: Path, expected_files: Optional[List[str]]
) -> List[str]:
    missing: List[str] = []
    for expected in expected_files or []:
        raw = str(expected or "").strip()
        if not raw:
            continue
        expects_dir = raw.endswith("/")
        candidate = (project_dir / raw.rstrip("/")).resolve()
        if not candidate.is_relative_to(project_dir):
            missing.append(raw)
            continue
        if expects_dir:
            if not candidate.exists() or not candidate.is_dir():
                missing.append(raw)
        elif not candidate.exists():
            missing.append(raw)
    return missing


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
        if not raw_command.strip():
            logger_obj.warning(
                f"[ISOLATION] Skipping blank command in {step_label} command {command_index}"
            )
            continue
        try:
            normalized_commands.append(_normalize_command(raw_command, project_dir))
        except TaskWorkspaceViolationError as exc:
            raise TaskWorkspaceViolationError(
                f"{step_label} command {command_index} blocked: {exc}. "
                f"Offending command: {raw_command}"
            ) from exc
    normalized_step["commands"] = normalized_commands

    raw_verification = str(step.get("verification") or "").strip()
    if raw_verification:
        try:
            normalized_step["verification"] = _normalize_command(
                raw_verification, project_dir
            )
        except TaskWorkspaceViolationError as exc:
            raise TaskWorkspaceViolationError(
                f"{step_label} verification blocked: {exc}. "
                f"Offending command: {raw_verification}"
            ) from exc
    else:
        normalized_step["verification"] = None

    raw_rollback = str(step.get("rollback") or "").strip()
    if raw_rollback:
        try:
            normalized_step["rollback"] = _normalize_command(raw_rollback, project_dir)
        except TaskWorkspaceViolationError as exc:
            raise TaskWorkspaceViolationError(
                f"{step_label} rollback blocked: {exc}. "
                f"Offending command: {raw_rollback}"
            ) from exc
    else:
        normalized_step["rollback"] = None

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


def _retry_planning_with_minimal_prompt(
    openclaw_service: Any,
    task_description: str,
    project_dir: Path,
    timeout_seconds: int,
    logger: logging.Logger,
    emit_live: Any,
    reason: str,
) -> Dict[str, Any]:
    """Retry planning with the strict JSON-only fallback prompt."""

    logger.warning(
        "[ORCHESTRATION] Planning output was not machine-parseable; "
        f"retrying with minimal prompt ({reason})"
    )
    emit_live(
        "WARN",
        "[ORCHESTRATION] Planning output needed a strict JSON retry",
        metadata={
            "phase": "planning",
            "retry": "minimal_prompt",
            "reason": reason[:240],
        },
    )
    minimal_planning_prompt = _build_minimal_planning_prompt(
        task_description, project_dir
    )
    return asyncio.run(
        openclaw_service.execute_task(
            minimal_planning_prompt,
            timeout_seconds=min(timeout_seconds, 240),
        )
    )


def _is_long_running_verification_task(
    execution_profile: str, step_description: str, task_prompt: str
) -> bool:
    combined = f"{execution_profile} {step_description} {task_prompt}".lower()
    verification_markers = (
        "verify",
        "verification",
        "refine",
        "integration",
        "end-to-end",
        "e2e",
        "test",
        "qa",
        "audit",
        "review",
        "build",
    )
    return execution_profile in {"test_only", "review_only"} or any(
        marker in combined for marker in verification_markers
    )


def _should_retry_planning_with_minimal_prompt(
    planning_result: Dict[str, Any], output_text: str = ""
) -> bool:
    error_text = (planning_result.get("error") or "").lower()
    combined_text = f"{error_text}\n{(output_text or '').lower()}"
    retry_markers = (
        "context window exceeded",
        "request timed out before a response was generated",
        "timed out",
        "timeout",
    )
    return any(marker in combined_text for marker in retry_markers)


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

        def emit_live(
            level: str, message: str, metadata: Optional[Dict[str, Any]] = None
        ) -> None:
            _record_live_log(
                db,
                session_id,
                task_id,
                level,
                message,
                session_instance_id=session.instance_id,
                metadata=metadata,
            )

        execution_profile = (
            getattr(task, "execution_profile", None)
            or getattr(session, "default_execution_profile", None)
            or "full_lifecycle"
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

        task_service = TaskService(db)
        runs_in_canonical_baseline = bool(
            project
            and task
            and _is_verification_style_task(
                execution_profile, task.title, task.description
            )
        )
        if runs_in_canonical_baseline and project:
            consolidation_result = task_service.rebuild_project_baseline(project)
            canonical_baseline_dir = task_service.get_project_baseline_dir(project)
            canonical_baseline_dir.mkdir(parents=True, exist_ok=True)
            orchestration_state._project_dir_override = str(canonical_baseline_dir)
            logger.info(
                "[ORCHESTRATION] Using canonical project baseline for task %s at %s",
                task_id,
                canonical_baseline_dir,
            )
            emit_live(
                "INFO",
                (
                    "[ORCHESTRATION] Consolidated completed work into canonical "
                    f"project baseline ({consolidation_result.get('files_copied', 0)} files) "
                    f"and will execute in {canonical_baseline_dir}"
                ),
                metadata={
                    "phase": "consolidation",
                    "baseline_path": str(canonical_baseline_dir),
                    "files_copied": consolidation_result.get("files_copied", 0),
                    "merged_task_count": consolidation_result.get(
                        "merged_task_count", 0
                    ),
                },
            )

        # Create the task workspace directory if it doesn't exist
        task_workspace = orchestration_state.project_dir
        if not os.path.exists(task_workspace):
            os.makedirs(task_workspace, exist_ok=True)
            logger.info(f"Created task workspace: {task_workspace}")

        is_resume_execution = bool(resume_checkpoint_name)

        hydration_result = (
            task_service.hydrate_task_workspace(
                project, task, orchestration_state.project_dir
            )
            if project and task
            else {"hydrated": False, "source_tasks": [], "files_copied": 0}
        )
        if hydration_result.get("hydrated"):
            logger.info(
                "[ORCHESTRATION] Hydrated workspace for task %s with %s files from prior tasks",
                task_id,
                hydration_result.get("files_copied", 0),
            )
            emit_live(
                "INFO",
                (
                    f"[ORCHESTRATION] Hydrated current workspace with {hydration_result.get('files_copied', 0)} "
                    "files from completed/promoted prior tasks"
                ),
                metadata={
                    "phase": "workspace_hydration",
                    "sources": hydration_result.get("source_tasks", []),
                },
            )

        workspace_snapshot_result: Optional[Dict[str, Any]] = None
        if project and not is_resume_execution:
            workspace_snapshot_result = _snapshot_workspace_before_run(
                task_service,
                project,
                task_id,
                orchestration_state.project_dir,
                preserve_project_root_rules=runs_in_canonical_baseline,
            )
            if workspace_snapshot_result is not None:
                emit_live(
                    "INFO",
                    (
                        "[ORCHESTRATION] Captured workspace snapshot before execution "
                        f"({workspace_snapshot_result.get('files_copied', 0)} files)"
                    ),
                    metadata={
                        "phase": "workspace_snapshot",
                        "snapshot_path": workspace_snapshot_result.get("snapshot_path"),
                        "source_exists": workspace_snapshot_result.get("source_exists"),
                    },
                )

        def restore_workspace_snapshot_if_needed(
            reason: str,
        ) -> Optional[Dict[str, Any]]:
            if not project:
                return None
            restore_result = _restore_workspace_after_abort(
                task_service,
                project,
                task_id,
                orchestration_state.project_dir,
                preserve_project_root_rules=runs_in_canonical_baseline,
            )
            if restore_result and restore_result.get("restored"):
                logger.warning(
                    "[ORCHESTRATION] Restored workspace snapshot for task %s after %s (%s files)",
                    task_id,
                    reason,
                    restore_result.get("files_restored", 0),
                )
                emit_live(
                    "WARN",
                    (
                        "[ORCHESTRATION] Restored workspace to the pre-run snapshot "
                        f"after {reason} ({restore_result.get('files_restored', 0)} files)"
                    ),
                    metadata={
                        "phase": "workspace_restore",
                        "reason": reason,
                        "snapshot_path": restore_result.get("snapshot_path"),
                        "target_path": restore_result.get("target_path"),
                    },
                )
            return restore_result

        project_context_summary = task_service.build_project_execution_context(
            project=project,
            current_task=task,
        )
        if hydration_result.get("hydrated"):
            hydrated_sources = ", ".join(
                f"#{item.get('task_id')} {item.get('title')}"
                for item in hydration_result.get("source_tasks", [])[:6]
            )
            project_context_summary = (
                f"{project_context_summary}\n"
                f"Hydrated baseline sources available directly in this workspace: {hydrated_sources}"
            )[:5000]
        orchestration_state.project_context = project_context_summary

        # Check if task has been running too long (safety check).
        # Skip this stale-run guard for explicit checkpoint resumes, otherwise a
        # legitimate resume after several minutes gets rejected before we even
        # load the saved orchestration state.
        if task.started_at and not is_resume_execution:
            time_since_start = datetime.utcnow() - task.started_at
            if time_since_start.total_seconds() > 300:  # 5 minutes
                logger.warning(
                    f"[ORCHESTRATION] Task {task_id} already running for {time_since_start}, marking as failed"
                )
                task.status = TaskStatus.FAILED
                task.error_message = f"Task already running for {time_since_start}, possible duplicate execution"
                db.commit()
                raise Exception("Task timeout - already running too long")
        elif task.started_at and is_resume_execution:
            logger.info(
                "[ORCHESTRATION] Skipping stale-run timeout guard for task %s because resume checkpoint '%s' was requested",
                task_id,
                resume_checkpoint_name,
            )

        # Update task status
        task.status = TaskStatus.RUNNING
        task.started_at = datetime.utcnow()
        task.completed_at = None
        task.error_message = None
        task.current_step = 0
        task.workspace_status = "in_progress" if task.task_subfolder else "not_created"
        session_task_link = _get_latest_session_task_link(db, session_id, task_id)
        if session_task_link:
            session_task_link.status = TaskStatus.RUNNING
            session_task_link.started_at = task.started_at
            session_task_link.completed_at = None
        db.commit()
        _write_project_state_snapshot(db, project, task, session_id)

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
            if checkpoint_context.get("project_dir_override"):
                orchestration_state._project_dir_override = checkpoint_context.get(
                    "project_dir_override"
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

        refreshed_project_context = task_service.build_project_execution_context(
            project=project,
            current_task=task,
        )
        if hydration_result.get("hydrated"):
            hydrated_sources = ", ".join(
                f"#{item.get('task_id')} {item.get('title')}"
                for item in hydration_result.get("source_tasks", [])[:6]
            )
            refreshed_project_context = (
                f"{refreshed_project_context}\n"
                f"Hydrated baseline sources available directly in this workspace: {hydrated_sources}"
            )[:5000]
        orchestration_state.project_context = refreshed_project_context

        gate_error = _run_virtual_merge_gate(
            db=db,
            project=project,
            current_task=task,
            execution_profile=execution_profile,
        )
        if gate_error:
            orchestration_state.status = OrchestrationStatus.ABORTED
            orchestration_state.abort_reason = gate_error
            task.status = TaskStatus.FAILED
            task.error_message = gate_error
            if session_task_link:
                session_task_link.status = TaskStatus.FAILED
                session_task_link.completed_at = datetime.utcnow()
            session.status = "paused"
            session.is_active = False
            _set_session_alert(session, "error", gate_error[:2000])
            db.commit()
            restore_workspace_snapshot_if_needed("virtual merge gate failure")
            _write_project_state_snapshot(db, project, task, session_id)
            return {"status": "failed", "reason": "virtual_merge_gate_failed"}

        if (
            not resumed_from_checkpoint
            and task
            and task.steps
            and not orchestration_state.plan
        ):
            try:
                stored_plan_payload = json.loads(task.steps)
                stored_plan = _extract_plan_steps(stored_plan_payload)
                if stored_plan:
                    orchestration_state.plan = _normalize_plan_with_live_logging(
                        db,
                        session_id,
                        task_id,
                        stored_plan,
                        orchestration_state.project_dir,
                        logger,
                        session.instance_id,
                        "Stored task plan",
                    )
                    logger.info(
                        "[ORCHESTRATION] Reusing stored plan for task %s with %s steps",
                        task_id,
                        len(orchestration_state.plan),
                    )
                    emit_live(
                        "INFO",
                        f"[ORCHESTRATION] Reusing saved plan with {len(orchestration_state.plan)} steps",
                        metadata={"phase": "planning", "source": "stored_task_plan"},
                    )
            except Exception as stored_plan_error:
                logger.warning(
                    "[ORCHESTRATION] Failed to reuse stored plan for task %s: %s",
                    task_id,
                    stored_plan_error,
                )

        if not resumed_from_checkpoint and not orchestration_state.plan:
            # PHASE 1: PLANNING - Generate step plan
            logger.info("[ORCHESTRATION] Phase 1: PLANNING - generating step plan")
            emit_live(
                "INFO",
                "[ORCHESTRATION] Phase 1: PLANNING - generating step plan",
                metadata={"phase": "planning"},
            )

            # Use project_name (already slugified) for the project context
            planning_prompt = PromptTemplates.build_planning_prompt(
                task_description=prompt,
                project_context=orchestration_state.project_context,
                workspace_root=str(orchestration_state.workspace_root),
                project_dir=str(orchestration_state.project_dir),
                execution_profile=execution_profile,
            )

            # Planning often needs more time than the old 300 second cap,
            # especially for larger repos or denser prompts.
            planning_timeout_seconds = max(240, min(timeout_seconds, 480))
            used_minimal_planning_prompt = False
            planning_result = asyncio.run(
                openclaw_service.execute_task(
                    planning_prompt, timeout_seconds=planning_timeout_seconds
                )
            )

            initial_output_text = _extract_structured_text(
                planning_result.get("output", "")
            )
            if _should_retry_planning_with_minimal_prompt(
                planning_result, initial_output_text
            ):
                logger.warning(
                    "[ORCHESTRATION] Planning failed on the first pass; retrying with minimal prompt"
                )
                emit_live(
                    "WARN",
                    "[ORCHESTRATION] Planning needed a fallback; retrying with minimal prompt",
                    metadata={
                        "phase": "planning",
                        "retry": "minimal_prompt",
                        "reason": (planning_result.get("error") or initial_output_text)[
                            :240
                        ],
                    },
                )
                planning_result = _retry_planning_with_minimal_prompt(
                    openclaw_service=openclaw_service,
                    task_description=prompt,
                    project_dir=orchestration_state.project_dir,
                    timeout_seconds=planning_timeout_seconds,
                    logger=logger,
                    emit_live=emit_live,
                    reason=(planning_result.get("error") or initial_output_text),
                )
                used_minimal_planning_prompt = True

            # Parse planning result to get steps
            try:
                while True:
                    output_result = planning_result.get("output", {})

                    # Debug: Log raw result to diagnose JSON parsing issues
                    logger.info(
                        f"[ORCHESTRATION] Planning result keys: {list(planning_result.keys()) if isinstance(planning_result, dict) else 'Not a dict'}"
                    )
                    logger.info(
                        f"[ORCHESTRATION] Planning output type: {type(output_result)}, preview: {str(output_result)[:300]}"
                    )
                    logger.info(
                        f"[ORCHESTRATION] Raw planning output type: {type(output_result)}, content preview: {str(output_result)[:200]}"
                    )

                    output_text = _extract_structured_text(output_result)
                    if not output_text.strip() and isinstance(output_result, dict):
                        output_text = json.dumps(output_result)
                        logger.info(
                            "[ORCHESTRATION] Structured text extraction empty; using full JSON"
                        )
                    elif isinstance(output_result, str):
                        logger.info("[ORCHESTRATION] Raw string response")
                    else:
                        logger.info(
                            f"[ORCHESTRATION] Structured text extracted from {type(output_result)}"
                        )

                    logger.info(
                        f"[ORCHESTRATION] Final extracted text length: {len(output_text)}"
                    )

                    if isinstance(output_text, str):
                        markdown_pattern = r"^\s*```(?:json)?\s*|\s*```$"
                        output_text = re.sub(markdown_pattern, "", output_text.strip())
                        logger.info(
                            f"[ORCHESTRATION] After stripping markdown, length: {len(output_text)}"
                        )

                    success, plan_data, strategy_info = (
                        error_handler.attempt_json_parsing(
                            output_text, context="planning"
                        )
                    )

                    if _should_retry_planning_with_minimal_prompt(
                        planning_result, output_text
                    ):
                        raise TimeoutError(
                            f"Planning timed out or exceeded context after {planning_timeout_seconds}s"
                        )

                    if not success and not used_minimal_planning_prompt:
                        planning_result = _retry_planning_with_minimal_prompt(
                            openclaw_service=openclaw_service,
                            task_description=prompt,
                            project_dir=orchestration_state.project_dir,
                            timeout_seconds=planning_timeout_seconds,
                            logger=logger,
                            emit_live=emit_live,
                            reason=f"json_parse_failed: {output_text[:240]}",
                        )
                        used_minimal_planning_prompt = True
                        continue

                    if not success:
                        orchestration_state.status = OrchestrationStatus.ABORTED
                        orchestration_state.abort_reason = (
                            f"Planning JSON parse failed: {strategy_info}"
                        )
                        emit_live(
                            "ERROR",
                            f"[ORCHESTRATION] Planning JSON parse failed: {strategy_info}",
                            metadata={
                                "phase": "planning",
                                "reason": "planning_json_error",
                            },
                        )
                        task.status = TaskStatus.FAILED
                        task.error_message = (
                            f"Planning JSON parse failed: {strategy_info}. "
                            f"Raw output: {output_text[:500]}"
                        )
                        db.commit()
                        restore_workspace_snapshot_if_needed(
                            "planning JSON parse failure"
                        )
                        return {"status": "failed", "reason": "planning_json_error"}

                    extracted_plan = _extract_plan_steps(plan_data)
                    if extracted_plan is None and not used_minimal_planning_prompt:
                        planning_result = _retry_planning_with_minimal_prompt(
                            openclaw_service=openclaw_service,
                            task_description=prompt,
                            project_dir=orchestration_state.project_dir,
                            timeout_seconds=planning_timeout_seconds,
                            logger=logger,
                            emit_live=emit_live,
                            reason=f"unexpected_plan_shape: {str(plan_data)[:240]}",
                        )
                        used_minimal_planning_prompt = True
                        continue

                    if (
                        _looks_like_truncated_multistep_plan(
                            output_text, extracted_plan
                        )
                        and not used_minimal_planning_prompt
                    ):
                        planning_result = _retry_planning_with_minimal_prompt(
                            openclaw_service=openclaw_service,
                            task_description=prompt,
                            project_dir=orchestration_state.project_dir,
                            timeout_seconds=planning_timeout_seconds,
                            logger=logger,
                            emit_live=emit_live,
                            reason="truncated_multistep_plan_detected",
                        )
                        used_minimal_planning_prompt = True
                        continue

                    if _looks_like_truncated_multistep_plan(
                        output_text, extracted_plan
                    ):
                        orchestration_state.status = OrchestrationStatus.ABORTED
                        orchestration_state.abort_reason = "Planning output collapsed a multi-step plan into a single step"
                        emit_live(
                            "ERROR",
                            "[ORCHESTRATION] Planning output was truncated into a single-step plan",
                            metadata={
                                "phase": "planning",
                                "reason": "truncated_multistep_plan_after_retry",
                            },
                        )
                        task.status = TaskStatus.FAILED
                        task.error_message = (
                            "Planning output collapsed a multi-step plan into a single "
                            "step after retry. The run was stopped to avoid a false "
                            "success."
                        )
                        db.commit()
                        restore_workspace_snapshot_if_needed(
                            "truncated multi-step plan"
                        )
                        return {
                            "status": "failed",
                            "reason": "truncated_multistep_plan_after_retry",
                        }

                    if extracted_plan is None:
                        plan_shape = type(plan_data).__name__
                        plan_keys = (
                            sorted(plan_data.keys())
                            if isinstance(plan_data, dict)
                            else []
                        )
                        raise ValueError(
                            "Planning result is not a recognized list of steps "
                            f"(type={plan_shape}, keys={plan_keys}, preview={str(plan_data)[:240]})"
                        )

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
                    break
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
                restore_workspace_snapshot_if_needed("workspace isolation violation")
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
                restore_workspace_snapshot_if_needed("planning parse error")
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
                restore_workspace_snapshot_if_needed(f"session {session.status}")
                _write_project_state_snapshot(db, project, task, session_id)
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

            if _step_needs_command_repair(step):
                repaired_step = None
                for repair_attempt in range(1, 3):
                    emit_live(
                        "WARN",
                        (
                            f"[ORCHESTRATION] Step {step_index + 1} has no runnable commands; "
                            f"attempting self-correction ({repair_attempt}/2)"
                        ),
                        metadata={
                            "phase": "step_validation",
                            "step_index": step_index + 1,
                            "attempt": repair_attempt,
                        },
                    )
                    repaired_step = _repair_step_commands_with_self_correction(
                        openclaw_service=openclaw_service,
                        db=db,
                        session_id=session_id,
                        task_id=task_id,
                        session_instance_id=session.instance_id if session else None,
                        task_prompt=prompt,
                        step=step,
                        step_index=step_index,
                        project_dir=orchestration_state.project_dir,
                        prior_results_summary=orchestration_state.prior_results_summary(),
                        project_context=orchestration_state.project_context,
                        logger_obj=logger,
                    )
                    if repaired_step is not None:
                        orchestration_state.plan[step_index] = repaired_step
                        step = repaired_step
                        task.steps = json.dumps(orchestration_state.plan)
                        _save_orchestration_checkpoint(
                            db, session_id, task_id, prompt, orchestration_state
                        )
                        db.commit()
                        break

                if repaired_step is None:
                    manual_gate_message = (
                        f"Step {step_index + 1} generated empty or invalid commands twice. "
                        "Manual review is required before execution can continue."
                    )
                    orchestration_state.status = OrchestrationStatus.ABORTED
                    orchestration_state.abort_reason = manual_gate_message
                    task.status = TaskStatus.FAILED
                    task.error_message = manual_gate_message
                    if session_task_link:
                        session_task_link.status = TaskStatus.FAILED
                        session_task_link.completed_at = datetime.utcnow()
                    session.status = "paused"
                    session.is_active = False
                    _set_session_alert(session, "error", manual_gate_message)
                    db.commit()
                    restore_workspace_snapshot_if_needed("manual review gate")
                    _write_project_state_snapshot(db, project, task, session_id)
                    return {"status": "failed", "reason": "manual_review_required"}

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
            execution_prompt = PromptTemplates.build_execution_prompt(
                step_description=step_description,
                step_commands=step_commands,
                project_dir=str(orchestration_state.project_dir),
                verification_command=verification_command,
                rollback_command=rollback_command,
                expected_files=expected_files,
                completed_steps_summary=orchestration_state.prior_results_summary(),
                project_context=orchestration_state.project_context,
                execution_profile=execution_profile,
            )

            # Give each step a workable minimum budget so larger scaffold/build
            # steps do not fail prematurely on otherwise healthy runs.
            if _is_long_running_verification_task(
                execution_profile, step_description, prompt
            ):
                step_timeout_seconds = max(600, min(timeout_seconds, 1800))
            else:
                step_timeout_seconds = max(
                    300,
                    timeout_seconds // max(1, min(len(orchestration_state.plan), 3)),
                )

            # Execute step
            step_started_at = datetime.utcnow()
            step_result = asyncio.run(
                openclaw_service.execute_task(
                    execution_prompt,
                    timeout_seconds=step_timeout_seconds,
                )
            )

            # Count attempts for this step (from debug_attempts history)
            step_debug_attempts = [
                da
                for da in orchestration_state.debug_attempts
                if da.get("attempt") is not None
                and da.get("step_index", -1) == step_index
            ]
            current_attempt = len(step_debug_attempts) + 1
            max_attempts = 3  # Maximum retry attempts per step

            # Record result
            step_output = step_result.get("output", "")
            step_status = (
                "success" if step_result.get("status") != "failed" else "failed"
            )

            if step_status == "success":
                missing_files = _missing_expected_files(
                    orchestration_state.project_dir, expected_files
                )
                if missing_files:
                    step_status = "failed"
                    missing_summary = ", ".join(missing_files[:6])
                    step_result["error"] = (
                        "Step reported success but expected files are missing: "
                        f"{missing_summary}"
                    )
                    emit_live(
                        "WARN",
                        (
                            f"[ORCHESTRATION] Step {step_index + 1} reported success but "
                            f"did not materialize expected files: {missing_summary}"
                        ),
                        metadata={
                            "phase": "executing",
                            "step_index": step_index + 1,
                            "missing_expected_files": missing_files[:20],
                        },
                    )

            tool_failures = _recent_step_tool_failures(
                db,
                session_id,
                task_id,
                step_started_at,
            )
            if step_status == "success" and tool_failures:
                step_status = "failed"
                failure_summary = " | ".join(tool_failures[:3])
                step_result["error"] = (
                    "Step reported success but task logs contain tool failures: "
                    f"{failure_summary}"
                )
                emit_live(
                    "WARN",
                    (
                        f"[ORCHESTRATION] Step {step_index + 1} reported success but "
                        "task logs contain tool failures"
                    ),
                    metadata={
                        "phase": "executing",
                        "step_index": step_index + 1,
                        "tool_failures": tool_failures[:10],
                    },
                )

            step_record = StepResult(
                step_number=step_index + 1,
                status=step_status,
                output=step_output[:1000],
                verification_output=step_result.get("verification_output", ""),
                files_changed=step_result.get("files_changed", expected_files),
                error_message=step_result.get("error", ""),
                attempt=current_attempt,
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

                # Check if we've exceeded max attempts
                if current_attempt >= max_attempts:
                    logger.warning(
                        f"[ORCHESTRATION] Step {step_index + 1} failed after {current_attempt} attempts (max: {max_attempts}), marking as failed"
                    )
                    emit_live(
                        "ERROR",
                        f"[ORCHESTRATION] Step {step_index + 1} failed after {current_attempt} attempts, marking as failed",
                        metadata={
                            "phase": "debugging",
                            "step_index": step_index + 1,
                            "max_attempts_reached": True,
                        },
                    )
                    orchestration_state.status = OrchestrationStatus.ABORTED
                    orchestration_state.abort_reason = (
                        f"Step {step_index + 1} failed after {current_attempt} attempts"
                    )
                    task.status = TaskStatus.FAILED
                    task.error_message = f"Step failed after {current_attempt} attempts: {step_record.error_message[:500]}"
                    db.commit()
                    restore_workspace_snapshot_if_needed("max step attempts reached")
                    _write_project_state_snapshot(db, project, task, session_id)
                    return {"status": "failed", "reason": "max_attempts_reached"}

                debug_prompt = PromptTemplates.build_debugging_prompt(
                    step_description=step_description,
                    error_message=step_record.error_message,
                    command_output=step_output,
                    verification_output=step_record.verification_output,
                    attempt_number=current_attempt,
                    max_attempts=max_attempts,
                    prior_debug_attempts=orchestration_state.debug_attempts,
                    project_name=orchestration_state.project_name,
                    workspace_root=str(orchestration_state.workspace_root),
                    project_dir=str(orchestration_state.project_dir),
                )

                debug_result = asyncio.run(
                    openclaw_service.execute_task(debug_prompt, timeout_seconds=180)
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
                            metadata={
                                "phase": "plan_revision",
                                "step_index": step_index + 1,
                            },
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
                                revise_prompt, timeout_seconds=180
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
                        # Apply the fix to the step before retrying
                        logger.info(
                            f"[ORCHESTRATION] Applying {fix_type} before retrying step {step_index + 1}"
                        )

                        # Store the fix attempt for history
                        orchestration_state.debug_attempts.append(
                            {
                                "attempt": len(orchestration_state.debug_attempts) + 1,
                                "fix_type": fix_type,
                                "fix": debug_data.get("fix", ""),
                                "analysis": debug_data.get("analysis", ""),
                                "confidence": debug_data.get("confidence", "MEDIUM"),
                                "error": step_record.error_message,
                            }
                        )

                        # If this is a command_fix, we need to modify the step_commands
                        # For code_fix, we let the LLM's execution prompt handle the fix
                        if fix_type == "command_fix" and debug_data.get("fix"):
                            # The fix contains the corrected command(s)
                            # Update the step's expected command for the retry
                            step_commands = [
                                debug_data.get(
                                    "fix", step_commands[0] if step_commands else ""
                                )
                            ]

                        emit_live(
                            "INFO",
                            f"[ORCHESTRATION] Fix applied ({fix_type}), retrying step {step_index + 1}",
                            metadata={
                                "phase": "debugging",
                                "step_index": step_index + 1,
                                "fix_type": fix_type,
                            },
                        )
                        # Continue to retry the step with the fix incorporated
                        continue  # Retry this step

                except TaskWorkspaceViolationError as e:
                    orchestration_state.status = OrchestrationStatus.ABORTED
                    orchestration_state.abort_reason = (
                        f"Workspace isolation violation: {e}"
                    )
                    task.status = TaskStatus.FAILED
                    task.error_message = str(e)
                    db.commit()
                    restore_workspace_snapshot_if_needed(
                        "debug workspace isolation violation"
                    )
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
                    restore_workspace_snapshot_if_needed("debug parse error")
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
            execution_profile=execution_profile,
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
        task.workspace_status = "ready" if task.task_subfolder else "not_created"
        if session_task_link:
            session_task_link.status = TaskStatus.DONE
            session_task_link.completed_at = task.completed_at

        baseline_publish_result = None
        if project and task.task_subfolder and not runs_in_canonical_baseline:
            baseline_publish_result = task_service.auto_publish_task_into_baseline(
                project, task
            )

        _set_session_alert(session, None, None)

        next_task = None
        blocked_pending_task = None
        if session and session.execution_mode == "automatic":
            next_task = _get_next_pending_project_task(db, session.project_id)
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
                blockers = TaskService(db).get_blocking_prior_tasks(
                    blocked_pending_task
                )
                if blockers:
                    blocking_summary = ", ".join(
                        f"#{item.plan_position} {item.title} ({item.status.value})"
                        for item in blockers[:3]
                    )
                    _set_session_alert(
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
        _write_project_state_snapshot(db, project, task, session_id)

        logger.info(
            f"[ORCHESTRATION] Task {task_id} completed successfully with {len(orchestration_state.plan)} steps"
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

        if session and next_task:
            next_session_task_link = _get_latest_session_task_link(
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
            execute_openclaw_task.delay(
                session_id=session_id,
                task_id=next_task.id,
                prompt=next_task.description or next_task.title,
                timeout_seconds=900,
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
            task.workspace_status = "blocked" if task.task_subfolder else "not_created"

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

        alert_message = (
            f"Task {task_id} failed in {session.execution_mode if session else 'session'} mode: {str(exc)}"
            if session
            else f"Task {task_id} failed: {str(exc)}"
        )

        if session:
            session.status = "paused"
            session.is_active = False
            _set_session_alert(session, "error", alert_message[:2000])

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

        try:
            if (
                project
                and orchestration_state
                and "restore_workspace_snapshot_if_needed" in locals()
            ):
                restore_workspace_snapshot_if_needed("task exception")
        except Exception as restore_error:
            logger.error(
                "[ORCHESTRATION] Failed to restore pre-run workspace snapshot for task %s: %s",
                task_id,
                str(restore_error),
            )

        db.commit()
        _write_project_state_snapshot(db, project, task, session_id)

        if session:
            db.add(
                LogEntry(
                    session_id=session_id,
                    session_instance_id=session.instance_id,
                    task_id=task_id,
                    level="ERROR",
                    message=alert_message[:2000],
                    log_metadata=json.dumps(
                        {
                            "alarm": True,
                            "execution_mode": session.execution_mode,
                            "task_id": task_id,
                        }
                    ),
                )
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
