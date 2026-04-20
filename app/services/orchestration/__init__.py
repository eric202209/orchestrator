"""Stable import surface for orchestration flows, helpers, and validators.

Keep this module focused on re-exporting the worker-facing orchestration API.
Internal cross-module calls inside the package should usually import directly
from the concrete module they need.
"""

from .completion_flow import finalize_successful_task
from .context_assembly import (
    assemble_completion_repair_inputs,
    assemble_execution_prompt,
    assemble_planning_prompt,
    build_workspace_inventory_summary,
    collect_workspace_inventory_paths,
)
from .execution_flow import (
    StepExecutionAssessment,
    ToolPathFailureDecision,
    assess_step_execution,
    determine_step_timeout,
    is_long_running_verification_task,
    missing_expected_files,
    repeated_tool_path_failure_decision,
)
from .execution_loop import execute_step_loop
from .executor import ExecutorService
from .failure_flow import handle_task_failure
from .parsing import (
    extract_plan_steps,
    extract_structured_text,
    looks_like_truncated_multistep_plan,
)
from .policy import (
    DEBUG_TIMEOUT_SECONDS,
    MAX_STEP_ATTEMPTS,
    MINIMAL_PLANNING_TIMEOUT_SECONDS,
    PLANNING_TIMEOUT_MAX_SECONDS,
    PLANNING_TIMEOUT_MIN_SECONDS,
    PLANNING_REPAIR_TIMEOUT_SECONDS,
    STALE_RUN_GUARD_SECONDS,
    SUMMARY_TIMEOUT_SECONDS,
    ULTRA_MINIMAL_PLANNING_TIMEOUT_SECONDS,
    clamp_planning_timeout,
    should_restore_workspace_on_failure,
)
from .persistence import (
    append_orchestration_event,
    record_live_log,
    record_validation_verdict,
    restore_step_result,
    save_orchestration_checkpoint,
    set_session_alert,
)
from .planner import PlannerService
from .planning_flow import execute_planning_phase
from .reporting import build_task_report_payload, render_task_report
from .runtime import (
    build_project_state_snapshot,
    build_workspace_discovery_step,
    get_state_manager_path,
    restore_workspace_after_abort,
    snapshot_workspace_before_run,
    write_project_state_snapshot,
)
from .step_support import (
    build_step_repair_prompt,
    coerce_execution_step_result,
    repair_step_commands_with_self_correction,
    step_needs_command_repair,
)
from .task_rules import (
    get_task_report_path,
    is_verification_style_task,
    run_virtual_merge_gate,
    should_execute_in_canonical_project_root,
    should_force_review_execution_profile,
)
from .telemetry import emit_phase_event, record_phase_event
from .types import OrchestrationRunContext, ValidationVerdict
from .validator import ValidatorService
from .workspace_guard import (
    TaskWorkspaceViolationError,
    normalize_plan_with_live_logging,
    normalize_step,
)

__all__ = [
    "ValidationVerdict",
    "PlannerService",
    "append_orchestration_event",
    "assemble_completion_repair_inputs",
    "assemble_execution_prompt",
    "assemble_planning_prompt",
    "build_workspace_inventory_summary",
    "collect_workspace_inventory_paths",
    "ExecutorService",
    "ValidatorService",
    "record_live_log",
    "record_validation_verdict",
    "restore_step_result",
    "save_orchestration_checkpoint",
    "set_session_alert",
    "build_project_state_snapshot",
    "build_workspace_discovery_step",
    "get_state_manager_path",
    "restore_workspace_after_abort",
    "snapshot_workspace_before_run",
    "write_project_state_snapshot",
    "build_step_repair_prompt",
    "coerce_execution_step_result",
    "repair_step_commands_with_self_correction",
    "step_needs_command_repair",
    "StepExecutionAssessment",
    "ToolPathFailureDecision",
    "assess_step_execution",
    "determine_step_timeout",
    "is_long_running_verification_task",
    "missing_expected_files",
    "repeated_tool_path_failure_decision",
    "TaskWorkspaceViolationError",
    "normalize_plan_with_live_logging",
    "normalize_step",
    "extract_plan_steps",
    "extract_structured_text",
    "looks_like_truncated_multistep_plan",
    "PLANNING_TIMEOUT_MIN_SECONDS",
    "PLANNING_TIMEOUT_MAX_SECONDS",
    "MINIMAL_PLANNING_TIMEOUT_SECONDS",
    "PLANNING_REPAIR_TIMEOUT_SECONDS",
    "ULTRA_MINIMAL_PLANNING_TIMEOUT_SECONDS",
    "STALE_RUN_GUARD_SECONDS",
    "MAX_STEP_ATTEMPTS",
    "DEBUG_TIMEOUT_SECONDS",
    "SUMMARY_TIMEOUT_SECONDS",
    "clamp_planning_timeout",
    "should_restore_workspace_on_failure",
    "get_task_report_path",
    "is_verification_style_task",
    "run_virtual_merge_gate",
    "should_execute_in_canonical_project_root",
    "should_force_review_execution_profile",
    "record_phase_event",
    "emit_phase_event",
    "OrchestrationRunContext",
    "build_task_report_payload",
    "render_task_report",
    "execute_planning_phase",
    "execute_step_loop",
    "finalize_successful_task",
    "handle_task_failure",
]
