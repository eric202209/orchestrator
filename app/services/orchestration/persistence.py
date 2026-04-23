"""Checkpoint, logging, validation, and event persistence helpers for orchestration."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.models import LogEntry, Session as SessionModel, TaskCheckpoint
from app.services.workspace.checkpoint_service import CheckpointService
from app.services.prompt_templates import OrchestrationState, StepResult

from .types import ValidationVerdict


def _orchestration_event_log_path(
    project_dir: Any, session_id: int, task_id: int
) -> Path:
    return (
        Path(project_dir)
        / ".openclaw"
        / "events"
        / f"session_{session_id}_task_{task_id}.jsonl"
    )


def append_orchestration_event(
    *,
    project_dir: Any,
    session_id: int,
    task_id: int,
    event_type: str,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = {
        "timestamp": datetime.utcnow().isoformat(),
        "event_type": event_type,
        "session_id": session_id,
        "task_id": task_id,
        "details": details or {},
    }
    log_path = _orchestration_event_log_path(project_dir, session_id, task_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, default=str) + "\n")
    return payload


def set_session_alert(
    session: Optional[SessionModel],
    level: Optional[str] = None,
    message: Optional[str] = None,
) -> None:
    if not session:
        return
    session.last_alert_level = level
    session.last_alert_message = message
    session.last_alert_at = datetime.utcnow() if message else None


def serialize_step_result(step_result: StepResult) -> Dict[str, Any]:
    return {
        "step_number": step_result.step_number,
        "status": step_result.status,
        "output": step_result.output,
        "verification_output": step_result.verification_output,
        "files_changed": step_result.files_changed,
        "error_message": step_result.error_message,
        "attempt": step_result.attempt,
    }


def restore_step_result(data: Dict[str, Any]) -> StepResult:
    return StepResult(
        step_number=data.get("step_number", 0),
        status=data.get("status", "failed"),
        output=data.get("output", ""),
        verification_output=data.get("verification_output", ""),
        files_changed=data.get("files_changed", []) or [],
        error_message=data.get("error_message", ""),
        attempt=data.get("attempt", 1),
    )


def save_orchestration_checkpoint(
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
            # Always persist the concrete resolved path so resume is stable
            # regardless of workspace_root recalculation.
            "project_dir_override": str(orchestration_state.project_dir),
            "workspace_path_override": (
                str(orchestration_state._workspace_path_override)
                if orchestration_state._workspace_path_override
                else None
            ),
        },
        orchestration_state={
            "status": orchestration_state.status.value,
            "plan": orchestration_state.plan,
            "current_step_index": orchestration_state.current_step_index,
            "debug_attempts": orchestration_state.debug_attempts,
            "changed_files": orchestration_state.changed_files,
            "validation_history": orchestration_state.validation_history,
            "phase_history": orchestration_state.phase_history,
            "last_plan_validation": orchestration_state.last_plan_validation,
            "last_completion_validation": orchestration_state.last_completion_validation,
            "relaxed_mode": orchestration_state.relaxed_mode,
            "completion_repair_attempts": orchestration_state.completion_repair_attempts,
            "execution_results": [
                serialize_step_result(r) for r in orchestration_state.execution_results
            ],
        },
        current_step_index=orchestration_state.current_step_index,
        step_results=[
            serialize_step_result(r) for r in orchestration_state.execution_results
        ],
    )


def record_validation_verdict(
    db: Session,
    session_id: int,
    task_id: int,
    orchestration_state: OrchestrationState,
    verdict: ValidationVerdict,
    *,
    step_number: Optional[int] = None,
) -> None:
    db.add(
        TaskCheckpoint(
            task_id=task_id,
            session_id=session_id,
            checkpoint_type=f"validation_{verdict.stage}",
            step_number=step_number,
            description=f"{verdict.stage}:{verdict.status}",
            state_snapshot=json.dumps(verdict.to_dict()),
        )
    )
    verdict_payload = verdict.to_dict()
    orchestration_state.validation_history.append(verdict_payload)
    try:
        append_orchestration_event(
            project_dir=orchestration_state.project_dir,
            session_id=session_id,
            task_id=task_id,
            event_type="validation_result",
            details={
                "stage": verdict.stage,
                "status": verdict.status,
                "profile": verdict.profile,
                "step_number": step_number,
                "reasons": verdict.reasons[:10],
            },
        )
    except Exception:
        pass
    if verdict.stage == "plan":
        orchestration_state.last_plan_validation = verdict_payload
    elif verdict.stage == "task_completion":
        orchestration_state.last_completion_validation = verdict_payload


def record_live_log(
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
