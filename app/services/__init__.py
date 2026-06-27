"""
Orchestrator services package
All services and utilities are available from this package
"""

import importlib as _importlib
import sys as _sys
import types as _types

_LEGACY_MODULE_ALIASES = {
    "auth_rate_limit": "app.services.auth.rate_limit",
    "authz": "app.services.auth.authorization",
    "build_identity": "app.services.observability.build_identity",
    "error_handler": "app.services.orchestration.error_handler",
    "file_lock": "app.services.workspace.file_lock",
    "github_service": "app.services.integrations.github",
    "health": "app.services.observability.health",
    "human_guidance_activation_service": "app.services.human_guidance.activation",
    "human_guidance_conflict_service": "app.services.human_guidance.conflicts",
    "human_guidance_plan_validator": "app.services.human_guidance.plan_validator",
    "human_guidance_post_write_checker": "app.services.human_guidance.post_write_checker",
    "human_guidance_selection_service": "app.services.human_guidance.selection",
    "human_guidance_service": "app.services.human_guidance.service",
    "log_stream_service": "app.services.observability.log_stream",
    "log_utils": "app.services.observability.log_utils",
    "name_formatter": "app.services.project.name_formatter",
    "performance_optimizations": "app.services.orchestration.prompt_optimization",
    "permission_service": "app.services.permissions.approval",
    "prompt_templates": "app.services.orchestration.prompt_templates",
    "streaming_health": "app.services.observability.streaming_health",
    "task_execution_service": "app.services.tasks.execution",
    "task_service": "app.services.tasks.service",
    "tool_tracking_service": "app.services.tasks.tool_tracking",
}


class _LegacyModuleAlias(_types.ModuleType):
    def __init__(self, legacy_name: str, target_name: str):
        super().__init__(legacy_name)
        self.__dict__["_target_name"] = target_name

    def _load(self):
        target = _importlib.import_module(self.__dict__["_target_name"])
        self.__dict__.update(target.__dict__)
        _sys.modules[self.__name__] = target
        return target

    def __getattr__(self, name: str):
        return getattr(self._load(), name)


for _legacy_name, _target_name in _LEGACY_MODULE_ALIASES.items():
    _full_legacy_name = f"{__name__}.{_legacy_name}"
    _sys.modules.setdefault(
        _full_legacy_name, _LegacyModuleAlias(_full_legacy_name, _target_name)
    )

from .agents import (
    AgentRuntime,
    create_agent_runtime,
)
from .tasks import TaskService, ToolTrackingService
from .permissions import PermissionApprovalService
from .observability import LogStreamService
from .integrations import GitHubService
from .planning import PlanCommitService, PlanningSessionService
from .orchestration.prompt_templates import PromptTemplates
from .session import (
    build_task_subfolder_name,
    ensure_task_workspace,
    ensure_unique_session_name,
    get_session_celery_task_ids,
    get_session_task_subfolder,
    maybe_queue_next_automatic_task,
    prepare_task_for_fresh_execution,
    queue_task_for_session,
    retry_session_with_stronger_planning_lane,
    reopen_failed_ordered_task_if_needed,
    revoke_session_celery_tasks,
    slugify_task_name,
    add_operator_guidance,
    approve_intervention,
    create_intervention_request,
    deny_intervention,
    get_intervention_history,
    get_pending_interventions,
    get_session_or_404,
    request_human_intervention_lifecycle,
    submit_intervention_reply,
    enrich_failure_summary_with_llm,
    get_or_generate_failure_summary,
    store_operator_feedback,
    trigger_replan,
    pause_session_lifecycle,
    resume_session_lifecycle,
    start_session_lifecycle,
    stop_session_lifecycle,
    derive_orchestration_state_block,
    check_session_overwrites_payload,
    cleanup_orphaned_checkpoints_payload,
    cleanup_session_checkpoints_payload,
    create_session_backup_payload,
    delete_session_checkpoint_payload,
    get_session_logs_payload,
    get_session_execution_dag_payload,
    get_session_divergence_compare_payload,
    get_session_dispatch_watchdog_payload,
    get_session_reconciliation_audit_payload,
    get_session_focus_mode_payload,
    get_session_mobile_interruptions_payload,
    get_session_timeline_payload,
    get_session_recovery_context_payload,
    get_session_digest_payload,
    get_session_state_diff_payload,
    get_session_trace_export_payload,
    get_session_workspace_info_payload,
    get_sorted_logs_payload,
    inspect_session_checkpoint_payload,
    list_session_checkpoints_payload,
    load_session_checkpoint_payload,
    replay_session_checkpoint_payload,
    replay_session_checkpoint_counterfactual_payload,
    refresh_session_dispatch_watchdog_alert,
    save_session_checkpoint_payload,
    execute_task_payload,
    get_session_statistics_payload,
    get_tool_execution_history_payload,
    start_agent_session_payload,
    start_openclaw_session_payload,
    start_session_payload,
    track_tool_execution_payload,
    stream_session_logs,
    stream_session_status,
)
from .workspace import ContextPreservationService, ProjectIsolationService

__all__ = [
    "AgentRuntime",
    "create_agent_runtime",
    "TaskService",
    "ContextPreservationService",
    "PermissionApprovalService",
    "ProjectIsolationService",
    "LogStreamService",
    "GitHubService",
    "ToolTrackingService",
    "PlanCommitService",
    "PlanningSessionService",
    "PromptTemplates",
    "build_task_subfolder_name",
    "ensure_task_workspace",
    "ensure_unique_session_name",
    "get_session_celery_task_ids",
    "get_session_task_subfolder",
    "maybe_queue_next_automatic_task",
    "prepare_task_for_fresh_execution",
    "queue_task_for_session",
    "retry_session_with_stronger_planning_lane",
    "reopen_failed_ordered_task_if_needed",
    "revoke_session_celery_tasks",
    "slugify_task_name",
    "add_operator_guidance",
    "approve_intervention",
    "create_intervention_request",
    "deny_intervention",
    "get_intervention_history",
    "get_pending_interventions",
    "get_session_or_404",
    "request_human_intervention_lifecycle",
    "submit_intervention_reply",
    "enrich_failure_summary_with_llm",
    "get_or_generate_failure_summary",
    "store_operator_feedback",
    "trigger_replan",
    "pause_session_lifecycle",
    "resume_session_lifecycle",
    "start_session_lifecycle",
    "stop_session_lifecycle",
    "derive_orchestration_state_block",
    "check_session_overwrites_payload",
    "cleanup_orphaned_checkpoints_payload",
    "cleanup_session_checkpoints_payload",
    "create_session_backup_payload",
    "delete_session_checkpoint_payload",
    "get_session_logs_payload",
    "get_session_execution_dag_payload",
    "get_session_divergence_compare_payload",
    "get_session_dispatch_watchdog_payload",
    "get_session_reconciliation_audit_payload",
    "get_session_focus_mode_payload",
    "get_session_mobile_interruptions_payload",
    "get_session_timeline_payload",
    "get_session_recovery_context_payload",
    "get_session_digest_payload",
    "get_session_state_diff_payload",
    "get_session_trace_export_payload",
    "get_session_workspace_info_payload",
    "get_sorted_logs_payload",
    "inspect_session_checkpoint_payload",
    "list_session_checkpoints_payload",
    "load_session_checkpoint_payload",
    "replay_session_checkpoint_payload",
    "replay_session_checkpoint_counterfactual_payload",
    "refresh_session_dispatch_watchdog_alert",
    "save_session_checkpoint_payload",
    "execute_task_payload",
    "get_session_statistics_payload",
    "get_tool_execution_history_payload",
    "start_agent_session_payload",
    "start_openclaw_session_payload",
    "start_session_payload",
    "track_tool_execution_payload",
    "stream_session_logs",
    "stream_session_status",
]
