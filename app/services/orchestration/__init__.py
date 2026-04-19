"""Stable import surface for orchestration flows, helpers, and validators.

Keep this module focused on re-exporting the worker-facing orchestration API.
Internal cross-module calls inside the package should usually import directly
from the concrete module they need.
"""

from .completion_flow import finalize_successful_task
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
from .persistence import (
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
    should_force_review_execution_profile,
)
from .types import ValidationVerdict
from .validator import ValidatorService
from .workspace_guard import (
    TaskWorkspaceViolationError,
    normalize_plan_with_live_logging,
    normalize_step,
)

__all__ = [
    "ValidationVerdict",
    "PlannerService",
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
    "get_task_report_path",
    "is_verification_style_task",
    "run_virtual_merge_gate",
    "should_force_review_execution_profile",
    "build_task_report_payload",
    "render_task_report",
    "execute_planning_phase",
    "execute_step_loop",
    "finalize_successful_task",
    "handle_task_failure",
]
