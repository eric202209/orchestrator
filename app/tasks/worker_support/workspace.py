"""Task Workspace restore helpers for orchestration workers."""

import logging
from typing import Any, Dict, Optional

from app.services.orchestration import should_restore_workspace_on_failure
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.execution.runtime import (
    restore_workspace_after_abort as _restore_workspace_after_abort,
)
from app.services.orchestration.state.persistence import (
    append_orchestration_event as _append_orchestration_event,
)

logger = logging.getLogger(__name__)


def _restore_workspace_snapshot_if_needed(
    reason: str,
    *,
    project: Any,
    session_id: int,
    task_id: int,
    task_execution_id: Optional[int],
    orchestration_state: Any,
    policy_profile_name: str,
    runs_in_canonical_baseline: bool,
    task_service: Any,
    emit_live: Any,
    force_restore: bool = False,
) -> Optional[Dict[str, Any]]:
    if not project:
        return None
    should_restore = should_restore_workspace_on_failure(
        reason, policy_profile=policy_profile_name
    )
    if not should_restore and not force_restore:
        logger.warning(
            "[ORCHESTRATION] Preserved workspace for task %s after %s; automatic restore is limited to isolation-violation failures",
            task_id,
            reason,
        )
        emit_live(
            "WARN",
            (
                "[ORCHESTRATION] Preserved the current workspace after "
                f"{reason}; automatic restore is only applied for "
                "workspace-isolation violations"
            ),
            metadata={
                "phase": "workspace_restore",
                "reason": reason,
                "restore_skipped": True,
                "policy": policy_profile_name,
            },
        )
        _append_orchestration_event(
            project_dir=orchestration_state.project_dir,
            session_id=session_id,
            task_id=task_id,
            event_type=EventType.WORKSPACE_RESTORE_SKIPPED,
            details={
                "reason": reason,
                "policy": "preserve_on_non_isolation_failures",
            },
        )
        return {
            "restored": False,
            "reason": "preserved_non_isolation_failure",
            "target_path": str(orchestration_state.project_dir),
        }
    restore_result = _restore_workspace_after_abort(
        task_service,
        project,
        task_id,
        orchestration_state.project_dir,
        task_execution_id=task_execution_id,
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
        _append_orchestration_event(
            project_dir=orchestration_state.project_dir,
            session_id=session_id,
            task_id=task_id,
            event_type=EventType.WORKSPACE_PRESERVED,
            details={
                "reason": reason,
                "restored_files": restore_result.get("files_restored", 0),
                "force_restore": force_restore,
            },
        )
    elif (
        restore_result
        and restore_result.get("reason")
        == "empty_snapshot_preserved_existing_workspace"
    ):
        logger.warning(
            "[ORCHESTRATION] Skipped destructive restore for task %s after %s because snapshot was empty and workspace already had files",
            task_id,
            reason,
        )
        emit_live(
            "WARN",
            (
                "[ORCHESTRATION] Skipped restoring an empty pre-run snapshot "
                f"after {reason} to preserve the current workspace "
                f"({restore_result.get('current_workspace_files', 0)} files)"
            ),
            metadata={
                "phase": "workspace_restore",
                "reason": reason,
                "snapshot_path": restore_result.get("snapshot_path"),
                "target_path": restore_result.get("target_path"),
                "current_workspace_files": restore_result.get(
                    "current_workspace_files", 0
                ),
                "restore_skipped": True,
            },
        )
        _append_orchestration_event(
            project_dir=orchestration_state.project_dir,
            session_id=session_id,
            task_id=task_id,
            event_type=EventType.WORKSPACE_RESTORE_SKIPPED,
            details={
                "reason": reason,
                "policy": "empty_snapshot_preserved_existing_workspace",
            },
        )
    return restore_result
