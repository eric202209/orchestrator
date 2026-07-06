"""Terminal verification-only failure assessment for the execution loop."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.services.orchestration.execution.step_dispatch import _get_task_execution
from app.services.orchestration.run_state import mark_task_attempt_failed
from app.services.orchestration.state.session_state import mark_session_failed


def _same_declared_command(command: Any, verification: Any) -> bool:
    return " ".join(str(command or "").strip().split()) == " ".join(
        str(verification or "").strip().split()
    )


def _is_terminal_verification_only_failure(
    *,
    step: Dict[str, Any],
    step_result: Dict[str, Any],
    step_status: str,
) -> bool:
    """Return true when the declared verification command itself is the task.

    A standalone verification step with no ops and no expected files has no
    workspace repair target. Retrying it through debug repair only asks the
    model to reinterpret or replace the task's explicit verification contract.
    """

    if step_status != "failed":
        return False
    if step.get("ops") or step.get("expected_files"):
        return False
    commands = [str(command or "").strip() for command in step.get("commands") or []]
    commands = [command for command in commands if command]
    verification = str(step.get("verification") or "").strip()
    if len(commands) != 1:
        return False
    if verification and not _same_declared_command(commands[0], verification):
        return False
    return bool(step_result.get("verification_output") or step_result.get("error"))


def _persist_terminal_execution_failure(
    *,
    db: Any,
    session: Any,
    task: Any,
    session_task_link: Any,
    task_execution_id: Optional[int],
    error_message: str,
    failure_category: str = "execution_failure",
) -> None:
    completed_at = datetime.now(timezone.utc)
    task_execution = _get_task_execution(db, task_execution_id)
    if task_execution is not None and not task_execution.failure_category:
        task_execution.failure_category = failure_category
    mark_task_attempt_failed(
        task=task,
        session_task_link=session_task_link,
        task_execution=task_execution,
        error_message=error_message,
        completed_at=completed_at,
        workspace_status="blocked",
    )
    mark_session_failed(
        session,
        failed_at=completed_at,
        alert_level="error",
        alert_message=error_message[:2000],
    )
    db.commit()
    try:
        from app.services.session.replan_service import get_or_generate_failure_summary

        get_or_generate_failure_summary(db, session.id)
    except Exception:
        pass
