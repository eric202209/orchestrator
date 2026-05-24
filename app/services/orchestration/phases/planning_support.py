"""Planning retry, diagnostics, and failure helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Dict

from app.models import TaskExecution, TaskStatus
from app.services.orchestration.context.assembly import compress_orchestration_context
from app.services.orchestration.planning.planner import (
    PlannerService,
    PlanningRepairNoOutputTimeout,
)
from app.services.orchestration.run_state import mark_task_attempt_failed
from app.services.orchestration.state.session_state import mark_session_paused
from app.services.orchestration.types import OrchestrationRunContext, ReasoningArtifact
from app.services.orchestration.validation.validator import MAX_PLANNING_COMMAND_CHARS

MAX_PLANNING_RETRIES = 3
TRUNCATED_PLAN_REPAIR_REJECTION_REASON = (
    "Output was cut off mid-stream. Ignore the broken output above. "
    "Produce a complete new JSON array from scratch."
)


def _truncated_multistep_collapse_diagnostics(
    *,
    output_text: str,
    extracted_plan: Any,
    repair_stage: str,
) -> dict[str, Any]:
    """Describe a collapsed multi-step response without changing policy."""

    text = output_text or ""
    raw_step_mentions = re.findall(
        r'(?:\\)?["\']step_number(?:\\)?["\']\s*:\s*(\d+)',
        text,
        flags=re.IGNORECASE,
    )
    step_mentions: list[int] = []
    for raw_step in raw_step_mentions:
        try:
            step_mentions.append(int(raw_step))
        except (TypeError, ValueError):
            continue

    description_mentions = len(
        re.findall(
            r'(?:\\)?["\']description(?:\\)?["\']\s*:',
            text,
            flags=re.IGNORECASE,
        )
    )
    original_step_count = max(
        len(set(step_mentions)),
        max(step_mentions) if step_mentions else 0,
        description_mentions,
        len(extracted_plan or []) if isinstance(extracted_plan, list) else 0,
    )

    absorbing_step = None
    if isinstance(extracted_plan, list) and extracted_plan:
        first_step = extracted_plan[0]
        if isinstance(first_step, dict):
            try:
                absorbing_step = int(first_step.get("step_number") or 1)
            except (TypeError, ValueError):
                absorbing_step = 1
        else:
            absorbing_step = 1

    subcodes = [
        (
            f"original_steps_detected_{original_step_count}"
            if original_step_count > 1
            else "original_steps_unknown"
        ),
        (
            f"absorbed_into_step_{absorbing_step}"
            if absorbing_step is not None
            else "absorbed_step_unknown"
        ),
        (
            "collapse_after_first_repair"
            if repair_stage == "after_first_repair"
            else "collapse_before_first_repair"
        ),
    ]
    return {
        "truncated_multistep_subcodes": subcodes,
        "truncated_multistep_original_step_count": (
            original_step_count if original_step_count > 1 else None
        ),
        "truncated_multistep_absorbing_step": absorbing_step,
        "truncated_multistep_repair_stage": repair_stage,
    }


def _normalized_step_numbers(raw_steps: Any) -> list[int]:
    step_numbers: list[int] = []
    if isinstance(raw_steps, dict):
        raw_iterable = raw_steps.keys()
    else:
        raw_iterable = raw_steps or []
    for raw_step in raw_iterable:
        try:
            step_numbers.append(int(raw_step))
        except (TypeError, ValueError):
            continue
    return sorted(set(step_numbers))


def _build_repair_rejection_reasons(
    reasons: list[str],
    verdict_details: Dict[str, Any] | None,
) -> list[str]:
    """Add precise validator diagnostics to repair without changing verdicts."""

    base_reasons = list(reasons or [])
    details = verdict_details or {}
    subcodes = set(details.get("brittle_command_subcodes") or [])
    targeted_reasons: list[str] = []

    step_lengths = details.get("brittle_command_step_command_lengths") or {}
    raw_steps = details.get("oversized_command_steps") or []
    step_numbers: list[int] = []
    if "oversized_command_length" in subcodes:
        step_numbers = _normalized_step_numbers(raw_steps)
    if not step_numbers:
        step_numbers = _normalized_step_numbers(step_lengths)
    if "oversized_command_length" in subcodes and step_numbers:
        lengths_by_step: list[str] = []
        for step_number in step_numbers:
            raw_lengths = step_lengths.get(step_number) or step_lengths.get(
                str(step_number)
            )
            lengths: list[int] = []
            for raw_length in raw_lengths or []:
                try:
                    lengths.append(int(raw_length))
                except (TypeError, ValueError):
                    continue
            if lengths:
                rendered_lengths = ", ".join(
                    str(length) for length in sorted(set(lengths))
                )
                lengths_by_step.append(f"step {step_number}: {rendered_lengths} chars")

        if lengths_by_step:
            length_clause = "; ".join(lengths_by_step)
        else:
            length_clause = (
                f"steps {step_numbers} exceed {MAX_PLANNING_COMMAND_CHARS} chars"
            )

        targeted_reasons.append(
            f"Step {step_numbers}: command body too long "
            f"(oversized_command_length; {length_clause}; max "
            f"{MAX_PLANNING_COMMAND_CHARS}). Rewrite as short printf/file edit "
            "commands. No heredoc."
        )

    if "multiple_heredoc_across_plan" in subcodes:
        try:
            heredoc_count = int(details.get("heredoc_command_count") or 0)
        except (TypeError, ValueError):
            heredoc_count = 0
        count_clause = (
            f"{heredoc_count} heredoc blocks found"
            if heredoc_count
            else "multiple heredoc blocks found"
        )
        targeted_reasons.append(
            f"Plan: {count_clause} (multiple_heredoc_across_plan). Rewrite file "
            "writes with printf. No heredoc."
        )

    step_details = details.get("brittle_command_step_details") or {}
    heredoc_subcodes = subcodes.intersection(
        {
            "disallowed_heredoc_shape",
            "multiple_heredoc_in_command",
            "looped_heredoc",
            "unsafe_heredoc_target",
            "markdown_wrapped_heredoc",
        }
    )
    if heredoc_subcodes:
        heredoc_steps = sorted(
            {
                step_number
                for step_number in _normalized_step_numbers(step_details)
                if heredoc_subcodes.intersection(
                    set(
                        step_details.get(step_number)
                        or step_details.get(str(step_number))
                        or []
                    )
                )
            }
        )
        step_label = f"Step {heredoc_steps}" if heredoc_steps else "Plan"
        targeted_reasons.append(
            f"{step_label}: invalid heredoc shape "
            f"({', '.join(sorted(heredoc_subcodes))}). Rewrite file writes with "
            "printf. No heredoc."
        )

    if "brittle_inline_python" in subcodes:
        inline_python_steps = sorted(
            {
                step_number
                for step_number in _normalized_step_numbers(step_details)
                if "brittle_inline_python"
                in set(
                    step_details.get(step_number)
                    or step_details.get(str(step_number))
                    or []
                )
            }
        )
        step_label = f"Step {inline_python_steps}" if inline_python_steps else "Plan"
        targeted_reasons.append(
            f"{step_label}: brittle inline Python (brittle_inline_python). "
            "Use ops for file content and verify Python with python -m py_compile, "
            "python -m unittest, or python -m pytest. If an import assertion is "
            "needed, create a tiny test file with ops instead of inline python -c."
        )

    if details.get("placeholder_only_implementation"):
        targeted_reasons.append(
            "placeholder_only_implementation: implementation steps look like stubs "
            "or placeholders. Replace TODO/pass/stub/touch-only content with real "
            "minimal behavior and verify it."
        )

    if "too_many_lines" in subcodes:
        too_many_line_steps = [
            step_number
            for step_number in _normalized_step_numbers(step_details)
            if "too_many_lines"
            in set(
                step_details.get(step_number)
                or step_details.get(str(step_number))
                or []
            )
        ]
        if too_many_line_steps:
            targeted_reasons.append(
                f"Step {too_many_line_steps}: command body too long "
                "(too_many_lines). Use ops write_file for file bodies. No heredoc."
            )

    weak_verification_steps = _normalized_step_numbers(
        details.get("weak_verification_steps") or []
    )
    if weak_verification_steps:
        targeted_reasons.append(
            f"weak_verification_steps: steps {weak_verification_steps} use weak "
            "verification commands; replace with pytest, python -m, or npm run build; "
            "python -c file/content assertion is also valid for static HTML."
        )

    missing_verification_steps = _normalized_step_numbers(
        details.get("missing_verification_steps") or []
    )
    if missing_verification_steps:
        targeted_reasons.append(
            "missing_verification_steps: steps "
            f"{missing_verification_steps} are missing verification commands; "
            "add pytest, python -m, npm run build, or an equivalent project test "
            "command that proves behavior for each implementation-heavy step."
        )

    missing_commands_steps = _normalized_step_numbers(
        details.get("missing_commands_steps") or []
    )
    if missing_commands_steps:
        targeted_reasons.append(
            "missing_commands_steps: steps "
            f"{missing_commands_steps} have no runnable command or file op; "
            "add a bounded shell command such as python -c, python -m, node -e, "
            "npm run, pytest, or an ops write_file/replace_in_file operation."
        )

    truncated_subcodes = details.get("truncated_multistep_subcodes") or []
    if truncated_subcodes:
        original_step_count = details.get("truncated_multistep_original_step_count")
        absorbing_step = details.get("truncated_multistep_absorbing_step")
        step_clause = (
            f"step {absorbing_step}" if absorbing_step is not None else "one step"
        )
        return_clause = (
            f"Return {original_step_count} separate step objects"
            if original_step_count
            else "Return separate step objects"
        )
        targeted_reasons.append(
            f"{return_clause}; do not merge into {step_clause}. "
            "truncated_multistep_subcodes: "
            f"{', '.join(str(code) for code in truncated_subcodes)}."
        )

    return targeted_reasons + base_reasons


def _brittle_command_diagnostic_details(
    verdict_details: Dict[str, Any] | None,
) -> dict[str, Any]:
    details = verdict_details or {}
    diagnostics: dict[str, Any] = {}
    subcodes = details.get("brittle_command_subcodes") or []
    if not subcodes:
        return diagnostics

    diagnostics["brittle_command_subcodes"] = list(subcodes)
    step_details = details.get("brittle_command_step_details") or {}
    if step_details:
        diagnostics["brittle_command_step_details"] = dict(step_details)
    return diagnostics


def _truncated_multistep_diagnostic_details(
    verdict_details: Dict[str, Any] | None,
) -> dict[str, Any]:
    details = verdict_details or {}
    subcodes = details.get("truncated_multistep_subcodes") or []
    if not subcodes:
        return {}
    return {
        "truncated_multistep_subcodes": list(subcodes),
        "truncated_multistep_original_step_count": details.get(
            "truncated_multistep_original_step_count"
        ),
        "truncated_multistep_absorbing_step": details.get(
            "truncated_multistep_absorbing_step"
        ),
        "truncated_multistep_repair_stage": details.get(
            "truncated_multistep_repair_stage"
        ),
    }


def _shadow_warning_details(
    verdict_details: Dict[str, Any] | None,
) -> dict[str, Any]:
    details = verdict_details or {}
    warnings = details.get("shadow_warnings") or []
    if not isinstance(warnings, list) or not warnings:
        return {}
    return {
        "shadow_warnings": [
            warning for warning in warnings[:10] if isinstance(warning, dict)
        ]
    }


def _plan_contract_diagnostics(
    verdict_details: Dict[str, Any] | None,
) -> dict[str, Any]:
    details = verdict_details or {}
    diagnostics = {
        key: details.get(key)
        for key in (
            "step_count",
            "max_command_length",
            "heredoc_command_count",
            "command_total_chars",
        )
    }
    diagnostics.update(_brittle_command_diagnostic_details(details))
    diagnostics.update(_truncated_multistep_diagnostic_details(details))
    diagnostics.update(_shadow_warning_details(details))
    return diagnostics


def _terminal_validation_failure_details(plan_verdict: Any) -> dict[str, Any]:
    details = {
        "reason": "planning_validation_failed_after_repair",
        "validation_reasons": list(plan_verdict.reasons or [])[:5],
    }
    details.update(_brittle_command_diagnostic_details(plan_verdict.details))
    details.update(_truncated_multistep_diagnostic_details(plan_verdict.details))
    details.update(_shadow_warning_details(plan_verdict.details))
    return details


def _post_repair_missing_verification_steps(plan_verdict: Any) -> list[int]:
    details = getattr(plan_verdict, "details", None) or {}
    missing_steps = _normalized_step_numbers(
        details.get("missing_verification_steps") or []
    )
    if not missing_steps:
        return []

    semantic_codes = set(details.get("semantic_violation_codes") or [])
    if semantic_codes and semantic_codes != {"missing_verification_command"}:
        return []

    blocking_detail_keys = (
        "weak_verification_steps",
        "brittle_command_subcodes",
        "placeholder_only_implementation",
        "non_runnable_steps",
        "background_process_steps",
        "nested_workspace_steps",
        "nested_project_root_steps",
        "malformed_shell_quoting_steps",
        "workflow_phase_violations",
        "stack_conflict",
    )
    if any(details.get(key) for key in blocking_detail_keys):
        return []

    reasons = [
        str(reason or "").lower()
        for reason in getattr(plan_verdict, "reasons", []) or []
    ]
    if reasons and any("missing verification" not in reason for reason in reasons):
        return []

    return missing_steps


def _post_repair_missing_command_steps(plan_verdict: Any) -> list[int]:
    details = getattr(plan_verdict, "details", None) or {}
    missing_steps = _normalized_step_numbers(
        details.get("missing_commands_steps") or []
    )
    if not missing_steps:
        return []

    blocking_detail_keys = (
        "missing_verification_steps",
        "weak_verification_steps",
        "brittle_command_subcodes",
        "placeholder_only_implementation",
        "non_runnable_steps",
        "background_process_steps",
        "nested_workspace_steps",
        "nested_project_root_steps",
        "malformed_shell_quoting_steps",
        "workflow_phase_violations",
        "stack_conflict",
    )
    if any(details.get(key) for key in blocking_detail_keys):
        return []

    reasons = [
        str(reason or "").lower()
        for reason in getattr(plan_verdict, "reasons", []) or []
    ]
    if reasons and any("without runnable commands" not in reason for reason in reasons):
        return []

    return missing_steps


def _post_repair_brittle_command_steps(plan_verdict: Any) -> list[int] | None:
    """Return step numbers eligible for brittle second repair, or None if blocked.

    Returns None when no brittle subcodes exist or other major issues are present
    (None = don't second-repair). Returns a list (possibly []) when brittle
    commands are the only remaining issue ([] = plan-level subcode, no step map).
    """

    details = getattr(plan_verdict, "details", None) or {}
    brittle_subcodes = details.get("brittle_command_subcodes") or []
    if not brittle_subcodes:
        return None

    # Don't second-repair for brittle commands when other major issues exist;
    # those need to be fixed first and the model must not be given conflicting instructions.
    blocking_detail_keys = (
        "missing_commands_steps",
        "missing_verification_steps",
        "placeholder_only_implementation",
        "non_runnable_steps",
        "background_process_steps",
        "nested_workspace_steps",
        "nested_project_root_steps",
        "workflow_phase_violations",
        "stack_conflict",
    )
    if any(details.get(key) for key in blocking_detail_keys):
        return None

    step_details = details.get("brittle_command_step_details") or {}
    return _normalized_step_numbers(step_details)


def _compress_project_context_for_planning(
    orchestration_state: Any,
    *,
    max_chars: int = 2800,
) -> str:
    current_context = str(getattr(orchestration_state, "project_context", "") or "")
    if len(current_context) <= max_chars:
        return current_context

    compact_state_summary = compress_orchestration_context(
        orchestration_state, max_chars=max_chars // 2
    )
    if compact_state_summary and compact_state_summary != "Step progress: 0/0":
        return compact_state_summary

    normalized = " ".join(current_context.split())
    if len(normalized) <= max_chars:
        return normalized

    head = normalized[: max_chars // 2].rstrip()
    tail = normalized[-(max_chars // 3) :].lstrip()
    return f"{head}\n...\n{tail}"


def _build_reasoning_artifact(
    *,
    ctx: OrchestrationRunContext,
    workspace_review: Dict[str, Any],
) -> Dict[str, Any]:
    plan = list(ctx.orchestration_state.plan or [])
    workspace_facts = [
        f"project_dir={ctx.orchestration_state.project_dir}",
        f"execution_profile={ctx.execution_profile}",
        f"workflow_profile={ctx.workflow_profile}",
    ]
    if workspace_review.get("has_existing_files"):
        workspace_facts.append("workspace already contains project files")
    file_count = int(workspace_review.get("file_count") or 0)
    source_file_count = int(workspace_review.get("source_file_count") or 0)
    if file_count or source_file_count:
        workspace_facts.append(
            f"workspace inventory shows {file_count} files and {source_file_count} source files"
        )
    review_summary = str(workspace_review.get("summary") or "").strip()
    if review_summary:
        workspace_facts.append(review_summary[:180])

    planned_actions = [
        str(step.get("description") or f"Step {index + 1}").strip()
        for index, step in enumerate(plan[:8])
        if str(step.get("description") or "").strip()
    ]
    verification_plan = []
    for step in plan[:8]:
        verification = str(step.get("verification") or "").strip()
        if verification:
            verification_plan.append(verification)
        elif step.get("expected_files"):
            verification_plan.append(
                "materialize expected files: "
                + ", ".join(
                    str(item) for item in (step.get("expected_files") or [])[:4]
                )
            )

    artifact = ReasoningArtifact(
        intent=" ".join(str(ctx.prompt or "").split())[:220],
        workspace_facts=workspace_facts[:8],
        planned_actions=planned_actions[:8],
        verification_plan=verification_plan[:8] or ["verify each planned step outcome"],
    )
    return artifact.to_dict()


def _should_repair_truncated_single_step_plan(
    *,
    prompt_profile: str,
    extracted_plan: list[dict[str, Any]] | None,
    execution_profile: str,
) -> bool:
    """Route compressed local-Qwen plans into repair instead of execution."""

    return (
        prompt_profile == "local_qwen_json_array"
        and execution_profile == "full_lifecycle"
        and isinstance(extracted_plan, list)
        and len(extracted_plan) == 1
    )


def _normalize_contract_violation_type(
    contract_violations: list[str] | None, default: str
) -> str:
    if not contract_violations:
        return default
    first = str(contract_violations[0] or "").strip().lower()
    if "multi-step prose" in first or "multi_step_prose" in first:
        return "multi_step_prose_summary"
    if "markdown" in first:
        return "markdown_wrapped_json"
    if "object wrapper" in first:
        return "object_wrapper_instead_of_top_level_array"
    if "extra keys" in first:
        return "extra_step_keys"
    if "missing" in first and "required" in first:
        return "missing_required_fields"
    if "background process" in first:
        return "background_process_command"
    if "non-runnable" in first:
        return "non_runnable_command"
    if "placeholder" in first:
        return "placeholder_only_step"
    normalized = re.sub(r"[^a-z0-9]+", "_", first).strip("_")
    return normalized[:80] or default


def _emit_planning_diagnostics_contract_violation(
    ctx: OrchestrationRunContext,
    *,
    reason: str,
    contract_violations: list[str] | None = None,
    semantic_violation_codes: list[str] | None = None,
    contract_diagnostics: dict[str, Any] | None = None,
    output_text: str = "",
    strategy_info: str = "",
) -> None:
    diagnostics = dict(contract_diagnostics or {})
    violation_type = _normalize_contract_violation_type(
        contract_violations, reason or "planning_contract_violation"
    )
    metadata = {
        "session_id": ctx.session_id,
        "task_id": ctx.task_id,
        "task_execution_id": ctx.task_execution_id,
        "contract_violation_type": violation_type,
        "reason": reason,
        "strategy_info": strategy_info,
        "output_chars": len(output_text or ""),
        "truncated_output_detected": (
            "truncated" in str(output_text or "").lower()
            or "truncated_multistep_plan" in str(reason or "")
        ),
        "contract_violations": list(contract_violations or [])[:8],
        "semantic_violation_codes": list(semantic_violation_codes or [])[:8],
        "step_count": diagnostics.get("step_count"),
        "max_command_length": diagnostics.get("max_command_length"),
        "heredoc_command_count": diagnostics.get("heredoc_command_count"),
        "command_total_chars": diagnostics.get("command_total_chars"),
    }
    metadata.update(_brittle_command_diagnostic_details(diagnostics))
    metadata.update(_truncated_multistep_diagnostic_details(diagnostics))
    metadata.update(_shadow_warning_details(diagnostics))
    ctx.emit_live(
        "WARN",
        "[OPENCLAW][PLANNING_DIAGNOSTICS] contract violation detected",
        metadata=metadata,
    )


def _semantic_codes_for_immediate_repair_issues(
    issues: dict[str, list[int]] | None,
) -> list[str]:
    codes: list[str] = []
    issue_map = {
        "non_runnable_steps": "non_runnable_command",
        "nested_workspace_steps": "nested_project_folder_command",
        "nested_project_root_steps": "nested_project_folder_command",
        "weak_verification_steps": "weak_verification",
        "missing_verification_steps": "missing_verification_command",
        "stale_replace_ops_steps": "stale_replace_in_file_old_text",
        "test_assertion_loss_ops_steps": "test_assertion_preservation_failed",
        "test_deletion_ops_steps": "test_preservation_violation",
    }
    for issue_key, code in issue_map.items():
        if (issues or {}).get(issue_key) and code not in codes:
            codes.append(code)
    return codes


def _model_lane_limitation_for_invalid_planning_commands(
    issues: dict[str, list[int]] | None,
) -> dict[str, object] | None:
    if not (issues or {}).get("stale_replace_ops_steps"):
        return None
    return {
        "model_lane_limitation": "repeated_stale_exact_patch_after_capsule",
        "failure_cause_bucket": "model_lane_repeated_stale_exact_patch",
        "runtime_rewrite_added": False,
        "recommended_action": (
            "Treat as planner/model-lane limitation. Use better planning context "
            "or scoped prompt guidance; do not add another runtime normalizer."
        ),
    }


def _is_repairable_malformed_shell_quoting_violation(exc: Exception) -> bool:
    message = str(exc).lower()
    return "malformed shell quoting" in message


class _PlanningRetryState:
    """Track retry/repair attempts to implement circuit breaking.

    persisted_failures: count of prior failed TaskExecution rows for this
    task/session loaded from DB at planning start.  Survives worker restarts
    so the circuit breaker cannot be reset to zero by a crash.
    """

    def __init__(self, persisted_failures: int = 0):
        self.consecutive_failures = 0
        self.persisted_failures = persisted_failures
        self.minimal_prompt_used = False
        self.repair_prompt_used = False
        self.post_repair_blocking_second_repair_used = False
        self.post_repair_validation_second_repair_used = False
        self.post_repair_malformed_shell_second_repair_used = False
        self.last_repair_reason = ""
        self.last_multistep_plan_step_count = 0

    @property
    def circuit_open(self) -> bool:
        return (
            self.consecutive_failures + self.persisted_failures >= MAX_PLANNING_RETRIES
        )


@dataclass(frozen=True)
class _SecondRepairReason:
    issue_key: str
    issue_label: str
    retry_reason: str
    event_reason: str
    semantic_violation_code: str
    step_numbers: list[int]
    rejection_text: str
    cap_used: bool
    cap_attribute: str


@dataclass(frozen=True)
class _SecondRepairPolicy:
    issue_key: str
    issue_label: str
    retry_reason: str
    event_reason: str
    semantic_violation_code: str
    cap_attribute: str
    rejection_template: str


_SECOND_REPAIR_BLOCKING_POLICIES: dict[str, _SecondRepairPolicy] = {
    "weak_verification_steps": _SecondRepairPolicy(
        issue_key="weak_verification_steps",
        issue_label="weak verification",
        retry_reason="post_repair_weak_verification_steps",
        event_reason="post_repair_weak_verification_second_pass",
        semantic_violation_code="weak_verification",
        cap_attribute="post_repair_blocking_second_repair_used",
        rejection_template=(
            "weak_verification_steps: steps {steps} still use weak verification "
            "after repair; replace each with pytest, python -m, or npm run build "
            "that proves behavior for the files changed in that step"
        ),
    ),
    "background_process_steps": _SecondRepairPolicy(
        issue_key="background_process_steps",
        issue_label="background process commands",
        retry_reason="post_repair_background_process_steps",
        event_reason="post_repair_background_process_second_pass",
        semantic_violation_code="background_process_command",
        cap_attribute="post_repair_blocking_second_repair_used",
        rejection_template=(
            "background_process_steps: steps {steps} still start background or "
            "long-running processes after repair; replace each with bounded "
            "foreground commands that terminate"
        ),
    ),
    "stale_replace_ops_steps": _SecondRepairPolicy(
        issue_key="stale_replace_ops_steps",
        issue_label="stale replace_in_file operations",
        retry_reason="post_repair_stale_replace_fallback",
        event_reason="post_repair_stale_replace_fallback_pass",
        semantic_violation_code="patch_strategy_fallback_required",
        cap_attribute="post_repair_blocking_second_repair_used",
        rejection_template=(
            "stale_replace_ops_steps: steps {steps} still use replace_in_file "
            "with old text that is absent from the current workspace. Exact-text "
            "patching is exhausted for these targets; do not emit another "
            "replace_in_file for the same missing old text. Use a targeted "
            "structured rewrite grounded in current file content, or ops.write_file "
            "with complete preserved file content as a last resort"
        ),
    ),
    "test_assertion_loss_ops_steps": _SecondRepairPolicy(
        issue_key="test_assertion_loss_ops_steps",
        issue_label="test assertion loss",
        retry_reason="post_repair_test_assertion_preservation",
        event_reason="post_repair_test_assertion_preservation_pass",
        semantic_violation_code="test_assertion_preservation_failed",
        cap_attribute="post_repair_blocking_second_repair_used",
        rejection_template=(
            "test_assertion_loss_ops_steps: steps {steps} rewrite an existing "
            "Python test file with fewer assertions. Preserve existing tests and "
            "assertion intent; do not replace behavioral checks with pass, stubs, "
            "tautologies, or weaker smoke checks"
        ),
    ),
    "test_deletion_ops_steps": _SecondRepairPolicy(
        issue_key="test_deletion_ops_steps",
        issue_label="test deletion",
        retry_reason="post_repair_test_deletion_preservation",
        event_reason="post_repair_test_deletion_preservation_pass",
        semantic_violation_code="test_preservation_violation",
        cap_attribute="post_repair_blocking_second_repair_used",
        rejection_template=(
            "test_deletion_ops_steps: steps {steps} delete existing Python test "
            "files. Do not delete tests during fallback repair; preserve the file "
            "and update only the minimal assertions/imports needed for the task"
        ),
    ),
}

_SECOND_REPAIR_VALIDATOR_POLICIES: dict[str, _SecondRepairPolicy] = {
    "missing_verification_steps": _SecondRepairPolicy(
        issue_key="missing_verification_steps",
        issue_label="missing verification",
        retry_reason="post_repair_missing_verification_steps",
        event_reason="post_repair_missing_verification_second_pass",
        semantic_violation_code="missing_verification_command",
        cap_attribute="post_repair_validation_second_repair_used",
        rejection_template=(
            "missing_verification_steps: steps {steps} are still missing "
            "verification after repair; add pytest, python -m, npm run build, "
            "or an equivalent project test command that proves behavior for "
            "each implementation-heavy step"
        ),
    ),
    "missing_commands_steps": _SecondRepairPolicy(
        issue_key="missing_commands_steps",
        issue_label="missing runnable commands",
        retry_reason="post_repair_missing_commands_steps",
        event_reason="post_repair_missing_commands_second_pass",
        semantic_violation_code="missing_runnable_command",
        cap_attribute="post_repair_validation_second_repair_used",
        rejection_template=(
            "missing_commands_steps: steps {steps} still have no runnable command "
            "or file op after repair; add a bounded command such as python -c, "
            "python -m, node -e, npm run, pytest, or an ops write_file/replace_in_file "
            "operation"
        ),
    ),
    "brittle_commands": _SecondRepairPolicy(
        issue_key="brittle_commands",
        issue_label="brittle heredoc or oversized commands",
        retry_reason="post_repair_brittle_commands",
        event_reason="post_repair_brittle_commands_second_pass",
        semantic_violation_code="brittle_heredoc_command",
        cap_attribute="post_repair_validation_second_repair_used",
        rejection_template=(
            "brittle_commands: steps {steps} still contain heredoc or oversized "
            "commands after repair; replace ALL file content writes with structured "
            'ops: [{{"op":"write_file","path":"...","content":"..."}}] — '
            "no cat heredoc, no printf multiline, no python -c with nested quotes"
        ),
    ),
}

_SECOND_REPAIR_WORKSPACE_POLICIES: dict[str, _SecondRepairPolicy] = {
    "malformed_shell_quoting": _SecondRepairPolicy(
        issue_key="malformed_shell_quoting",
        issue_label="malformed shell quoting",
        retry_reason="post_repair_malformed_shell_quoting",
        event_reason="post_repair_malformed_shell_quoting_second_pass",
        semantic_violation_code="malformed_shell_quoting",
        cap_attribute="post_repair_malformed_shell_second_repair_used",
        rejection_template=(
            "Malformed shell quoting: emit one valid shell command string; "
            "avoid unmatched quotes, mixed quote escaping, and python -c "
            "snippets with nested quotes"
        ),
    ),
}


def _second_repair_reason_from_policy(
    retry_state: _PlanningRetryState,
    policy: _SecondRepairPolicy,
    step_numbers: list[int],
) -> _SecondRepairReason:
    issue_steps = step_numbers[:5]
    return _SecondRepairReason(
        issue_key=policy.issue_key,
        issue_label=policy.issue_label,
        retry_reason=policy.retry_reason,
        event_reason=policy.event_reason,
        semantic_violation_code=policy.semantic_violation_code,
        step_numbers=issue_steps,
        rejection_text=policy.rejection_template.format(steps=issue_steps),
        cap_used=bool(getattr(retry_state, policy.cap_attribute)),
        cap_attribute=policy.cap_attribute,
    )


def _get_targeted_second_repair_reason(
    *,
    retry_state: _PlanningRetryState,
    blocking_repair_issues: dict[str, list[int]] | None = None,
    plan_verdict: Any | None = None,
    malformed_shell_quoting_violation: bool = False,
) -> _SecondRepairReason | None:
    if not retry_state.repair_prompt_used:
        return None

    if malformed_shell_quoting_violation:
        policy = _SECOND_REPAIR_WORKSPACE_POLICIES["malformed_shell_quoting"]
        return _second_repair_reason_from_policy(retry_state, policy, [])

    issue_keys = set((blocking_repair_issues or {}).keys())
    if len(issue_keys) == 1:
        issue_key = next(iter(issue_keys))
        policy = _SECOND_REPAIR_BLOCKING_POLICIES.get(issue_key)
        if policy:
            return _second_repair_reason_from_policy(
                retry_state,
                policy,
                (blocking_repair_issues or {}).get(issue_key) or [],
            )

    missing_verification_steps = (
        _post_repair_missing_verification_steps(plan_verdict) if plan_verdict else []
    )
    if missing_verification_steps:
        policy = _SECOND_REPAIR_VALIDATOR_POLICIES["missing_verification_steps"]
        return _second_repair_reason_from_policy(
            retry_state,
            policy,
            missing_verification_steps,
        )

    missing_command_steps = (
        _post_repair_missing_command_steps(plan_verdict) if plan_verdict else []
    )
    if missing_command_steps:
        policy = _SECOND_REPAIR_VALIDATOR_POLICIES["missing_commands_steps"]
        return _second_repair_reason_from_policy(
            retry_state,
            policy,
            missing_command_steps,
        )

    brittle_steps = (
        _post_repair_brittle_command_steps(plan_verdict) if plan_verdict else None
    )
    if brittle_steps is not None:
        # None = blocked (other issues exist or no brittle subcodes).
        # [] = plan-level brittle subcode with no per-step map; still trigger repair.
        # [1, 2] = specific steps with brittle commands.
        policy = _SECOND_REPAIR_VALIDATOR_POLICIES["brittle_commands"]
        return _second_repair_reason_from_policy(
            retry_state,
            policy,
            brittle_steps,
        )

    return None


def _classify_planning_timeout_failure(
    exc: Exception,
    retry_state: _PlanningRetryState | None,
) -> str:
    message = str(exc).lower()
    if PlannerService.is_openclaw_lock_contention(message):
        return "planning_openclaw_lock_contention"
    if "context" in message or "context overflow" in message:
        return "planning_context_overflow"
    if (
        retry_state
        and "repair" in message
        and (retry_state.repair_prompt_used or bool(retry_state.last_repair_reason))
    ):
        if isinstance(exc, PlanningRepairNoOutputTimeout) or "no output" in message:
            return "planning_repair_no_output_timeout"
        if any(
            marker in (retry_state.last_repair_reason or "")
            for marker in (
                "json_parse_failed",
                "unexpected_plan_shape",
                "truncated_multistep_plan",
                "plan_contains_immediate_repair_issues",
            )
        ):
            return "malformed_planning_output_repair_timeout"
        return "planning_repair_timeout"
    return "planning_timeout"


def _finalize_planning_terminal_failure(
    *,
    ctx: OrchestrationRunContext,
    failure_type: str,
    failure_reason: str,
    generate_failure_summary: bool = False,
) -> bool:
    completed_at = datetime.now(UTC)
    task_execution = None
    if ctx.task_execution_id:
        task_execution = (
            ctx.db.query(TaskExecution)
            .filter(TaskExecution.id == ctx.task_execution_id)
            .first()
        )
    mark_task_attempt_failed(
        task=ctx.task,
        session_task_link=ctx.session_task_link,
        task_execution=task_execution,
        error_message=failure_reason,
        completed_at=completed_at,
    )
    if ctx.session:
        mark_session_paused(
            ctx.session,
            alert_level="error",
            alert_message=failure_reason[:2000],
            paused_at=completed_at,
        )
    ctx.db.commit()
    if generate_failure_summary:
        try:
            from app.services.session.replan_service import (
                get_or_generate_failure_summary,
            )

            get_or_generate_failure_summary(ctx.db, ctx.session_id)
        except Exception as summary_exc:
            ctx.logger.debug(
                "[ORCHESTRATION] Failed to create/update failure summary for session=%s: %s",
                ctx.session_id,
                summary_exc,
            )

    knowledge_recorded = False
    try:
        from app.services.orchestration.phases.failure_flow import (
            record_failure_knowledge_for_stopped_session,
        )

        knowledge_recorded = bool(
            record_failure_knowledge_for_stopped_session(
                db=ctx.db,
                session_id=ctx.session_id,
                task_id=ctx.task_id,
                failure_reason=failure_type,
                logger=ctx.logger,
            )
        )
    except Exception as knowledge_exc:
        ctx.logger.warning(
            "[ORCHESTRATION] session_id=%s task_id=%s failure_type=%s "
            "handle_task_failure_called=False knowledge_recorded=False error=%s",
            ctx.session_id,
            ctx.task_id,
            failure_type,
            knowledge_exc,
        )
        return False

    ctx.logger.warning(
        "[ORCHESTRATION] session_id=%s task_id=%s failure_type=%s "
        "handle_task_failure_called=False knowledge_recorded=%s",
        ctx.session_id,
        ctx.task_id,
        failure_type,
        knowledge_recorded,
    )
    return knowledge_recorded


def _finalize_planning_timeout_failure(
    *,
    ctx: OrchestrationRunContext,
    failure_type: str,
    failure_reason: str,
) -> bool:
    return _finalize_planning_terminal_failure(
        ctx=ctx,
        failure_type=failure_type,
        failure_reason=failure_reason,
        generate_failure_summary=True,
    )


def _last_plan_output_snippet(planning_result: dict, max_chars: int = 400) -> str:
    output = planning_result.get("output", "")
    if isinstance(output, dict):
        text = str(output.get("text", "") or output.get("content", "") or "")
    else:
        text = str(output or "")
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
    return text


def _count_prior_failed_planning_executions(ctx: OrchestrationRunContext) -> int:
    if not ctx.db or not ctx.task_id or not ctx.session_id:
        return 0
    try:
        query = ctx.db.query(TaskExecution).filter(
            TaskExecution.task_id == ctx.task_id,
            TaskExecution.session_id == ctx.session_id,
            TaskExecution.status == TaskStatus.FAILED,
        )
        if ctx.task_execution_id:
            query = query.filter(TaskExecution.id < ctx.task_execution_id)
        count = query.count()
        return count if isinstance(count, int) else 0
    except Exception:
        return 0
