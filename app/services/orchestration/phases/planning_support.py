"""Planning retry, diagnostics, and failure helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict

from app.models import TaskExecution, TaskStatus
from app.services.orchestration.context.assembly import compress_orchestration_context
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.events.telemetry import emit_phase_event
from app.services.orchestration.planning.planner import (
    PlannerService,
    PlanningRepairNoOutputTimeout,
)
from app.services.orchestration.planning.source_materialization import (
    plan_has_concrete_source_materialization,
)
from app.services.orchestration.run_state import mark_task_attempt_failed
from app.services.orchestration.state.persistence import append_orchestration_event
from app.services.orchestration.state.session_state import mark_session_paused
from app.services.orchestration.types import OrchestrationRunContext, ReasoningArtifact
from app.services.orchestration.validation.validator import (
    MAX_PLANNING_COMMAND_CHARS,
    ValidatorService,
)
from app.services.prompt_templates import OrchestrationStatus

MAX_PLANNING_RETRIES = 3
TRUNCATED_PLAN_REPAIR_REJECTION_REASON = (
    "Output was cut off mid-stream. Ignore the broken output above. "
    "Produce a complete new JSON array from scratch."
)


def _usable_knowledge_context(knowledge_context: Any) -> Any:
    return (
        knowledge_context
        if knowledge_context and knowledge_context.retrieved_items
        else None
    )


def _planning_validation_profile(ctx: OrchestrationRunContext) -> str:
    try:
        return ValidatorService.infer_validation_profile(
            ctx.prompt,
            ctx.execution_profile,
            title=ctx.task.title if ctx.task else None,
            description=ctx.task.description if ctx.task else None,
        )
    except Exception:
        return ""


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
        placeholder_ops = [
            item
            for item in (details.get("placeholder_source_write_ops") or [])
            if isinstance(item, dict)
        ]
        for item in placeholder_ops[:3]:
            path = str(item.get("path") or "").strip()
            excerpt = " ".join(str(item.get("content_excerpt") or "").split())[:220]
            if not path:
                continue
            targeted_reasons.append(
                "placeholder_only_implementation source write: preserve source "
                f"write path `{path}`; replace placeholder/stub content with real "
                "implementation; do not convert package imports to `src.*` imports; "
                "do not remove materializing source operations. "
                f"Offending content excerpt: {excerpt}"
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

    if details.get("missing_materialization_for_implementation"):
        targeted_reasons.append(
            "missing_source_materialization: implementation-heavy plans must "
            "include at least one concrete source edit in the existing package. "
            "Use ops write_file or replace_in_file for the real source file named "
            "by tests/source context; inspect/test-only steps are not enough."
        )

    task1_bootstrap_contract = details.get("task1_bootstrap_contract")
    if isinstance(task1_bootstrap_contract, dict):
        required_artifacts = [
            str(path or "").strip()
            for path in task1_bootstrap_contract.get("required_artifacts") or []
            if str(path or "").strip()
        ]
        required_source_files = [
            str(path or "").strip()
            for path in task1_bootstrap_contract.get("required_source_files") or []
            if str(path or "").strip()
        ]
        required_test_files = [
            str(path or "").strip()
            for path in task1_bootstrap_contract.get("required_test_files") or []
            if str(path or "").strip()
        ]
        required_verification = [
            str(command or "").strip()
            for command in task1_bootstrap_contract.get("required_verification") or []
            if str(command or "").strip()
        ]
        package_markers = [
            str(path or "").strip()
            for path in task1_bootstrap_contract.get("python_package_markers") or []
            if str(path or "").strip()
        ]
        forbidden_src_imports = [
            str(import_name or "").strip()
            for import_name in (
                task1_bootstrap_contract.get("forbidden_python_src_imports") or []
            )
            if str(import_name or "").strip()
        ]
        violation_codes = [
            str(code or "").strip()
            for code in task1_bootstrap_contract.get("violation_codes") or []
            if str(code or "").strip()
        ]
        if violation_codes:
            targeted_reasons.append(
                "task1_bootstrap_contract: Repair must satisfy the same "
                "TaskBootstrapContract payload that caused validation rejection; "
                f"violation_codes={violation_codes[:8]}; "
                f"required_artifacts={required_artifacts[:8]}; "
                f"required_source_files={required_source_files[:8]}; "
                f"required_test_files={required_test_files[:8]}; "
                f"required_verification={required_verification[:4]}; "
                f"python_package_markers={package_markers[:8]}; "
                f"forbidden_python_src_imports={forbidden_src_imports[:8]}. "
                "Do not drop required tests or package markers. For Python "
                "src-layout tests, import the package namespace, not `src.*`."
            )

    missing_commands_steps = _normalized_step_numbers(
        details.get("missing_commands_steps") or []
    )
    if missing_commands_steps:
        targeted_reasons.append(
            "missing_commands_steps: steps "
            f"{missing_commands_steps} have no runnable command or file op; "
            "add a bounded shell command such as python -c, python -m, node -e, "
            "npm run, pytest, or an ops write_file/replace_in_file operation. "
            "If the step already has a valid verification command, copy that "
            "same command into commands instead of leaving commands empty."
        )

    physical_src_details = [
        item
        for item in (details.get("physical_src_import_details") or [])
        if isinstance(item, dict)
    ]
    if details.get("physical_src_import_materializations"):
        invalid_lines: list[str] = []
        for item in physical_src_details[:5]:
            for line in item.get("invalid_imports") or []:
                line_text = str(line or "").strip()
                if line_text and line_text not in invalid_lines:
                    invalid_lines.append(line_text)
        invalid_clause = (
            " Invalid import line(s): " + "; ".join(invalid_lines[:5])
            if invalid_lines
            else ""
        )
        targeted_reasons.append(
            "physical_src_import: Do not use `src.` as a Python import prefix in "
            "src-layout projects. Keep tests importing package paths such as "
            "`from math_tools.operations import add`; create or edit "
            "`src/math_tools/operations.py` instead of rewriting tests to "
            "`from src.math_tools import ...`." + invalid_clause
        )

    undefined_python_test_files = [
        str(path or "").strip()
        for path in (details.get("undefined_python_test_name_materializations") or [])
        if str(path or "").strip()
    ]
    if undefined_python_test_files:
        targeted_reasons.append(
            "undefined_python_test_names: Repair the source behavior instead of "
            "adding broken tests. Preserve existing tests as the contract; do not "
            "write tests with undefined helper names, undeclared fixtures, or "
            "`src.`-prefixed imports. If a test file must be touched, every name "
            "must be imported, defined, or provided by pytest fixtures. Offending "
            f"test file(s): {undefined_python_test_files[:5]}"
        )

    undefined_python_decorator_files = [
        str(path or "").strip()
        for path in (details.get("undefined_python_decorator_materializations") or [])
        if str(path or "").strip()
    ]
    if undefined_python_decorator_files:
        targeted_reasons.append(
            "framework_mismatch: Plan writes Python decorators whose root name is "
            "undefined. Preserve the framework already present in existing source "
            "and tests; for argparse CLIs, implement behavior in the existing "
            "parser/build_parser/main flow instead of inventing Typer/Click/"
            "FastAPI/Django decorators such as @app.command or @router.*. "
            "Offending source file(s): "
            f"{undefined_python_decorator_files[:5]}"
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
        "planning_root_cause": _planning_root_cause_from_plan_verdict(plan_verdict),
    }
    details.update(_brittle_command_diagnostic_details(plan_verdict.details))
    details.update(_truncated_multistep_diagnostic_details(plan_verdict.details))
    details.update(_shadow_warning_details(plan_verdict.details))
    return details


PLANNING_ROOT_CAUSES = {
    "invalid_python",
    "missing_verification",
    "missing_source_materialization",
    "stale_replace",
    "framework_mismatch",
    "retry_exhausted",
    "repair_timeout",
    "unknown",
}


def _normalize_planning_root_cause(value: Any) -> str:
    root_cause = str(value or "").strip()
    return root_cause if root_cause in PLANNING_ROOT_CAUSES else "unknown"


def _planning_root_cause_from_issue_key(issue_key: str | None) -> str:
    if issue_key in ("stale_replace_ops_steps", "empty_replace_old_text_steps"):
        return "stale_replace"
    if issue_key == "weak_verification_steps":
        return "missing_verification"
    return "unknown"


def _planning_root_cause_from_immediate_repair_issues(
    immediate_repair_issues: dict[str, Any] | None,
) -> str:
    issues = immediate_repair_issues or {}
    if issues.get("stale_replace_ops_steps") or issues.get(
        "empty_replace_old_text_steps"
    ):
        return "stale_replace"
    if issues.get("weak_verification_steps"):
        return "missing_verification"
    return "unknown"


def _planning_root_cause_from_plan_verdict(plan_verdict: Any) -> str:
    details = getattr(plan_verdict, "details", None) or {}
    codes = {
        str(code or "").strip().lower()
        for code in details.get("semantic_violation_codes") or []
    }
    reasons = "\n".join(
        str(reason or "") for reason in getattr(plan_verdict, "reasons", []) or []
    )
    text = (json.dumps(details, default=str, sort_keys=True) + "\n" + reasons).lower()
    if "python_source_syntax_invalid" in codes or "python source syntax" in text:
        return "invalid_python"
    if details.get("missing_verification_steps") or "missing verification" in text:
        return "missing_verification"
    if "patch_strategy_fallback_required" in codes or "stale_replace" in text:
        return "stale_replace"
    if (
        details.get("undefined_python_decorator_materializations")
        or "framework_mismatch" in text
        or "undefined decorator" in text
    ):
        return "framework_mismatch"
    if (
        details.get("missing_source_materialization")
        or "missing_source_materialization" in codes
        or "source materialization" in text
    ):
        return "missing_source_materialization"
    return "unknown"


def _repair_root_cause_from_plan_verdict(plan_verdict: Any) -> str:
    details = getattr(plan_verdict, "details", None) or {}
    reasons = "\n".join(
        str(reason or "") for reason in getattr(plan_verdict, "reasons", []) or []
    )
    text = (json.dumps(details, default=str, sort_keys=True) + "\n" + reasons).lower()
    planning_root_cause = _planning_root_cause_from_plan_verdict(plan_verdict)
    if planning_root_cause != "unknown":
        return planning_root_cause
    if "source_api_regression" in text or "missing_required_symbols" in text:
        return "source_api_regression"
    if "placeholder or stub" in text or "placeholder_only_steps" in text:
        return "placeholder_stub"
    return "unknown"


def _repair_root_cause_from_arbitration(arbitration: dict[str, Any]) -> str:
    labels = {
        str(label or "").strip() for label in arbitration.get("regression_labels") or []
    }
    source_api = arbitration.get("source_api_contract") or {}
    python_syntax = arbitration.get("python_syntax") or {}
    if arbitration.get("invalid_output") or python_syntax.get("status") in {
        "regressed",
        "still_invalid",
    }:
        return "invalid_python"
    if (
        "source_api_regression" in labels
        or source_api.get("status") == "regressed"
        or source_api.get("missing_required_symbols")
    ):
        return "source_api_regression"
    if "stale_replace" in labels:
        return "stale_replace"
    if "framework_drift" in labels:
        return "framework_mismatch"
    if "removed_materialization" in labels:
        return "missing_source_materialization"
    if "removed_verification" in labels:
        return "missing_verification"
    return "unknown"


def _record_planning_root_cause(retry_state: Any, root_cause: Any) -> str:
    normalized = _normalize_planning_root_cause(root_cause)
    if normalized != "unknown":
        retry_state.planning_root_cause = normalized
    return getattr(retry_state, "planning_root_cause", "unknown")


def _record_repair_root_cause(
    retry_state: Any,
    *,
    root_cause: Any,
    stage: str,
    progress: bool = False,
) -> None:
    normalized = str(root_cause or "").strip() or "unknown"
    if normalized == "unknown":
        return
    if _normalize_planning_root_cause(normalized) != "unknown":
        _record_planning_root_cause(retry_state, normalized)
    sequence = getattr(retry_state, "repair_root_cause_sequence", [])
    stage_sequence = getattr(retry_state, "repair_stage_sequence", [])
    if not sequence or sequence[-1] != normalized:
        sequence.append(normalized)
        stage_sequence.append(str(stage or "unknown"))
    retry_state.repair_root_cause_sequence = sequence
    retry_state.repair_stage_sequence = stage_sequence
    if progress:
        retry_state.repair_progress_observed = True


def _root_cause_oscillation_details(
    retry_state: Any,
    *,
    latest_progress: bool = False,
) -> dict[str, Any] | None:
    if latest_progress:
        return None
    sequence = [
        str(item)
        for item in getattr(retry_state, "repair_root_cause_sequence", []) or []
        if str(item or "").strip() and str(item or "") != "unknown"
    ]
    distinct = list(dict.fromkeys(sequence))
    if len(distinct) < 2:
        return None
    return {
        "reason": "root_cause_oscillation_no_progress",
        "cross_stage_convergence_class": "root_cause_oscillation",
        "oscillation_detected": True,
        "oscillation_root_causes": distinct,
        "oscillation_stage_sequence": list(
            getattr(retry_state, "repair_stage_sequence", []) or []
        ),
        "oscillation_action": "stop_repair_loop",
    }


def _verifier_failures_decreased_materially(
    *,
    previous_failure_count: int | None,
    current_failure_count: int | None,
) -> bool:
    if previous_failure_count is None or current_failure_count is None:
        return False
    return current_failure_count < previous_failure_count


def _terminal_planning_root_cause(
    retry_state: Any,
    *,
    fallback: str = "retry_exhausted",
) -> str:
    root_cause = _normalize_planning_root_cause(
        getattr(retry_state, "planning_root_cause", None)
    )
    if root_cause != "unknown":
        return root_cause
    return _normalize_planning_root_cause(fallback)


def _repeated_physical_src_import_repair_details(
    plan_verdict: Any,
) -> dict[str, Any] | None:
    details = getattr(plan_verdict, "details", None) or {}
    files = details.get("physical_src_import_materializations") or []
    if not files:
        return None
    invalid_lines: list[str] = []
    for item in details.get("physical_src_import_details") or []:
        if not isinstance(item, dict):
            continue
        for line in item.get("invalid_imports") or []:
            line_text = str(line or "").strip()
            if line_text and line_text not in invalid_lines:
                invalid_lines.append(line_text)
    return {
        "reason": "repeated_physical_src_import",
        "physical_src_import_materializations": list(files)[:10],
        "invalid_imports": invalid_lines[:10],
    }


def _abort_repeated_physical_src_import_repair(
    *,
    ctx: OrchestrationRunContext,
    plan_verdict: Any,
    output_text: str,
) -> dict[str, str] | None:
    details = _repeated_physical_src_import_repair_details(plan_verdict)
    if not details:
        return None

    failure_type = "repeated_physical_src_import"
    ctx.orchestration_state.status = OrchestrationStatus.ABORTED
    ctx.orchestration_state.abort_reason = (
        "Planning repair repeated physical src-prefixed Python imports"
    )
    _emit_planning_diagnostics_contract_violation(
        ctx,
        reason=failure_type,
        contract_violations=plan_verdict.reasons,
        semantic_violation_codes=["physical_src_import"],
        contract_diagnostics=details,
        output_text=output_text,
        strategy_info=failure_type,
    )
    emit_phase_event(
        ctx.orchestration_state,
        ctx.emit_live,
        level="ERROR",
        phase="planning",
        message=(
            "[ORCHESTRATION] Planning repair repeated a physical "
            "src-prefixed Python import"
        ),
        details={
            **details,
            "validation_reasons": list(plan_verdict.reasons or [])[:5],
        },
    )
    invalid_imports = details.get("invalid_imports") or []
    failure_reason = (
        "Planning repair repeated physical src-prefixed Python imports after "
        "explicit repair guidance"
    )
    if invalid_imports:
        failure_reason += ": " + "; ".join(str(line) for line in invalid_imports[:4])
    _finalize_planning_terminal_failure(
        ctx=ctx,
        failure_type=failure_type,
        failure_reason=failure_reason,
    )
    if ctx.restore_workspace_snapshot_if_needed:
        ctx.restore_workspace_snapshot_if_needed("repeated physical src import")
    return {
        "status": "failed",
        "reason": failure_type,
    }


def _abort_missing_source_materialization_repair(
    *,
    ctx: OrchestrationRunContext,
    retry_state: Any,
    output_text: str,
) -> dict[str, str] | None:
    if not (
        retry_state.repair_prompt_used
        and retry_state.source_materialization_required_after_repair
        and not plan_has_concrete_source_materialization(
            ctx.orchestration_state.plan,
            ctx.orchestration_state.project_dir,
        )
    ):
        return None

    failure_type = "planning_repair_missing_source_materialization"
    root_cause = _record_planning_root_cause(
        retry_state,
        "missing_source_materialization",
    )
    ctx.orchestration_state.status = OrchestrationStatus.ABORTED
    ctx.orchestration_state.abort_reason = (
        "Planning repair removed required source materialization"
    )
    emit_phase_event(
        ctx.orchestration_state,
        ctx.emit_live,
        level="ERROR",
        phase="planning",
        message=(
            "[ORCHESTRATION] Planning repair did not include a concrete source "
            "materialization"
        ),
        details={
            "reason": failure_type,
            "repair_reason": retry_state.last_repair_reason,
            "planning_root_cause": root_cause,
        },
    )
    _emit_planning_diagnostics_contract_violation(
        ctx,
        reason=failure_type,
        contract_violations=[
            "planning_repair_missing_source_materialization: repaired "
            "implementation-heavy plan must include at least one concrete source "
            "edit under src/ or the existing project package; inspect-only, "
            "test-only, and verification-only repairs are not acceptable"
        ],
        semantic_violation_codes=["missing_source_materialization"],
        contract_diagnostics={
            "repair_reason": retry_state.last_repair_reason,
            "planning_root_cause": root_cause,
        },
        output_text=output_text,
        strategy_info=failure_type,
    )
    _finalize_planning_terminal_failure(
        ctx=ctx,
        failure_type=failure_type,
        failure_reason=(
            "Planning repair for an implementation-heavy task did not include "
            "concrete source materialization under src/ or the existing project "
            "package."
        ),
        planning_root_cause=root_cause,
    )
    if ctx.restore_workspace_snapshot_if_needed:
        ctx.restore_workspace_snapshot_if_needed(
            "planning repair missing source materialization"
        )
    return {"status": "failed", "reason": failure_type}


def _abort_root_cause_oscillation_repair_loop(
    *,
    ctx: OrchestrationRunContext,
    retry_state: Any,
) -> dict[str, str] | None:
    details = _root_cause_oscillation_details(retry_state)
    if not details:
        return None

    failure_type = "root_cause_oscillation_no_progress"
    ctx.orchestration_state.status = OrchestrationStatus.ABORTED
    ctx.orchestration_state.abort_reason = (
        "Planning repair root cause oscillated without execution or verifier "
        "progress"
    )
    emit_phase_event(
        ctx.orchestration_state,
        ctx.emit_live,
        level="ERROR",
        phase="planning",
        message=(
            "[ORCHESTRATION] Planning repair root cause oscillated without "
            "progress; stopping repair loop"
        ),
        details={
            **details,
            "planning_root_cause": _terminal_planning_root_cause(retry_state),
        },
    )
    try:
        append_orchestration_event(
            project_dir=ctx.orchestration_state.project_dir,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            event_type=EventType.CROSS_STAGE_CONVERGENCE,
            details={
                **details,
                "planning_root_cause": _terminal_planning_root_cause(retry_state),
            },
        )
    except Exception as exc:
        ctx.logger.debug(
            "[ORCHESTRATION] Failed to persist root-cause oscillation event: %s",
            exc,
        )
    _finalize_planning_terminal_failure(
        ctx=ctx,
        failure_type=failure_type,
        failure_reason=(
            "Planning repair root cause oscillated without progress: "
            + " -> ".join(details["oscillation_root_causes"])
        ),
        planning_root_cause=_terminal_planning_root_cause(retry_state),
    )
    if ctx.restore_workspace_snapshot_if_needed:
        ctx.restore_workspace_snapshot_if_needed("root cause oscillation")
    return {"status": "failed", "reason": failure_type}


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
        "empty_replace_old_text_steps": "empty_replace_old_text",
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


def _extract_stale_old_text_from_plan(
    plan: list | None,
    stale_step_numbers: list[int] | None,
) -> list[str]:
    """Return the `old` text values from stale replace_in_file ops in the plan.

    Used only to surface operator evidence in the rerun payload.
    Must not be injected into model prompts.
    """
    if not plan or not stale_step_numbers:
        return []
    stale_set = set(stale_step_numbers)
    texts: list[str] = []
    for step in plan:
        if not isinstance(step, dict):
            continue
        if step.get("step_number") not in stale_set:
            continue
        for op in step.get("ops") or []:
            if op.get("op") == "replace_in_file" and "old" in op:
                texts.append(op["old"])
    return texts


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
        self.post_repair_stale_replace_second_repair_used = False
        self.post_repair_validation_second_repair_used = False
        self.post_repair_malformed_shell_second_repair_used = False
        self.post_repair_python_source_syntax_second_repair_used = False
        self.post_repair_framework_second_repair_used = False
        self.post_repair_task1_bootstrap_second_repair_used = False
        self.task1_bootstrap_rejection_contract: dict[str, Any] | None = None
        self.source_materialization_required_after_repair = False
        self.vma_repair_triggered = False
        self.last_repair_reason = ""
        self.last_multistep_plan_step_count = 0
        self.planning_root_cause = "unknown"
        self.repair_root_cause_sequence: list[str] = []
        self.repair_stage_sequence: list[str] = []
        self.repair_progress_observed = False

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
        cap_attribute="post_repair_stale_replace_second_repair_used",
        rejection_template=(
            "stale_replace_ops_steps: steps {steps} still use replace_in_file "
            "with old text that is absent from the current workspace. Exact-text "
            "patching is exhausted for these targets; do not emit another "
            "replace_in_file for the same missing old text or same target. Use "
            "ops.write_file with complete preserved file content grounded in the "
            "current file excerpt. write_file.content must be a JSON string; "
            "escape newline characters as \\n; do not use raw triple-quoted Python "
            "blocks; do not place bare multiline code outside JSON string quotes; "
            "the output must remain a valid JSON array"
        ),
    ),
    "empty_replace_old_text_steps": _SecondRepairPolicy(
        issue_key="empty_replace_old_text_steps",
        issue_label="replace_in_file operations with missing old text",
        retry_reason="post_repair_empty_replace_fallback",
        event_reason="post_repair_empty_replace_fallback_pass",
        semantic_violation_code="patch_strategy_fallback_required",
        cap_attribute="post_repair_stale_replace_second_repair_used",
        rejection_template=(
            "empty_replace_old_text_steps: steps {steps} use replace_in_file "
            "without specifying old text to search for. Exact-text patching "
            "cannot be applied without an anchor; do not emit another "
            "replace_in_file for the same target without complete, literal search "
            "text. Use ops.write_file with complete preserved file content "
            "grounded in the current file excerpt. write_file.content must be a "
            "JSON string; escape newline characters as \\n; do not use raw "
            "triple-quoted Python blocks; do not place bare multiline code outside "
            "JSON string quotes; the output must remain a valid JSON array"
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
    "task1_bootstrap_contract": _SecondRepairPolicy(
        issue_key="task1_bootstrap_contract",
        issue_label="Task-1 bootstrap contract",
        retry_reason="post_repair_task1_bootstrap_contract",
        event_reason="post_repair_task1_bootstrap_contract_second_pass",
        semantic_violation_code="task1_bootstrap_contract",
        cap_attribute="post_repair_task1_bootstrap_second_repair_used",
        rejection_template=(
            "task1_bootstrap_contract: repaired Task 1 still violates the "
            "bootstrap contract. Preserve or restore every required source file, "
            "test file, package marker, and verification command. Python "
            "src-layout tests must import the package namespace, not `src.*`"
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


# Replace-fallback keys and which co-occurring blocking issues prevent it.
# stale + empty can trigger patch_strategy_fallback_required even alongside
# compatible co-issues (weak verification, background process). They are blocked
# only when incompatible structural issues coexist that require different repair.
_REPLACE_FALLBACK_KEYS: frozenset[str] = frozenset(
    {"stale_replace_ops_steps", "empty_replace_old_text_steps"}
)
_REPLACE_FALLBACK_INCOMPATIBLE_KEYS: frozenset[str] = frozenset(
    {
        "non_runnable_steps",
        "placeholder_only_steps",
        "test_assertion_loss_ops_steps",
        "test_deletion_ops_steps",
    }
)

BLOCKING_IMMEDIATE_REPAIR_ISSUE_KEYS: tuple[str, ...] = (
    "non_runnable_steps",
    "background_process_steps",
    "placeholder_only_steps",
    "weak_verification_steps",
    "stale_replace_ops_steps",
    "empty_replace_old_text_steps",
    "test_assertion_loss_ops_steps",
    "test_deletion_ops_steps",
)


def _get_targeted_second_repair_reason(
    *,
    retry_state: _PlanningRetryState,
    blocking_repair_issues: dict[str, list[int]] | None = None,
    plan_verdict: Any | None = None,
    malformed_shell_quoting_violation: bool = False,
    project_dir: Path | None = None,
) -> _SecondRepairReason | None:
    if not retry_state.repair_prompt_used:
        return None

    if malformed_shell_quoting_violation:
        policy = _SECOND_REPAIR_WORKSPACE_POLICIES["malformed_shell_quoting"]
        return _second_repair_reason_from_policy(retry_state, policy, [])

    framework_mismatch_reason = _post_repair_argparse_framework_mismatch_reason(
        retry_state, plan_verdict, project_dir
    )
    if framework_mismatch_reason:
        return framework_mismatch_reason

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

    # Allow replace fallback when co-occurring with compatible issues.
    # Fires only when no incompatible structural issue is present.
    replace_keys_present = issue_keys & _REPLACE_FALLBACK_KEYS
    if replace_keys_present and not (issue_keys & _REPLACE_FALLBACK_INCOMPATIBLE_KEYS):
        # stale_replace_ops_steps takes priority over empty_replace_old_text_steps
        # since it has more specific workspace context to include in the prompt.
        issue_key = (
            "stale_replace_ops_steps"
            if "stale_replace_ops_steps" in replace_keys_present
            else "empty_replace_old_text_steps"
        )
        policy = _SECOND_REPAIR_BLOCKING_POLICIES[issue_key]
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

    python_source_syntax_reason = _post_repair_python_source_syntax_reason(
        retry_state, plan_verdict
    )
    if python_source_syntax_reason:
        return python_source_syntax_reason

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

    task1_bootstrap_reason = _post_repair_task1_bootstrap_contract_reason(
        retry_state,
        plan_verdict,
    )
    if task1_bootstrap_reason:
        return task1_bootstrap_reason

    return None


def _post_repair_task1_bootstrap_contract_reason(
    retry_state: _PlanningRetryState,
    plan_verdict: Any | None,
) -> _SecondRepairReason | None:
    if not plan_verdict:
        return None
    details = getattr(plan_verdict, "details", None) or {}
    contract = details.get("task1_bootstrap_contract")
    if not isinstance(contract, dict) or contract.get("passed") is not False:
        return None

    policy = _SECOND_REPAIR_VALIDATOR_POLICIES["task1_bootstrap_contract"]
    return _second_repair_reason_from_policy(retry_state, policy, [])


def _task1_bootstrap_second_repair_rejection_reasons(
    *,
    retry_state: _PlanningRetryState,
    plan_verdict: Any,
    rejection_text: str,
) -> list[str]:
    details = dict(getattr(plan_verdict, "details", None) or {})
    details["task1_bootstrap_contract"] = (
        retry_state.task1_bootstrap_rejection_contract
        or details.get("task1_bootstrap_contract")
    )
    return _build_repair_rejection_reasons(
        [rejection_text, *list(getattr(plan_verdict, "reasons", []) or [])],
        details,
    )


def _post_repair_argparse_framework_mismatch_reason(
    retry_state: _PlanningRetryState,
    plan_verdict: Any | None,
    project_dir: Path | None,
) -> _SecondRepairReason | None:
    if not plan_verdict or project_dir is None:
        return None
    details = getattr(plan_verdict, "details", None) or {}
    paths = [
        str(path or "").strip().lstrip("./")
        for path in (details.get("undefined_python_decorator_materializations") or [])
        if str(path or "").strip()
    ]
    if not paths:
        return None

    root = Path(project_dir).resolve()
    for path in paths:
        if not path.endswith(".py"):
            continue
        source_path = (root / path).resolve()
        try:
            if not source_path.is_relative_to(root):
                continue
            source_text = source_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not _source_text_is_argparse_cli(source_text):
            continue
        excerpt = _framework_source_excerpt(source_text)
        required_symbols = _argparse_public_symbols_to_preserve(source_text)
        symbol_clause = (
            ", ".join(required_symbols)
            if required_symbols
            else "existing public CLI symbols"
        )
        rejection_text = (
            "framework_mismatch: repaired Python source introduced decorator-style "
            "CLI code into an argparse module. "
            f"Affected file: {path}. detected framework: argparse. "
            f"Required public symbols to preserve when present: {symbol_clause}. "
            f"Source excerpt showing current argparse flow: {excerpt} "
            "Return a valid JSON array only. Use canonical ops.write_file with "
            "complete valid Python source content or ops.replace_in_file with exact "
            "current text. Do not introduce @cli.command, @click.command, "
            "@click.option, @app.command, @router.*, click.echo, or typer.*. "
            "Modify build_parser() and main(argv=None) only, or use complete "
            "write_file preserving the existing public API. add a summary subparser "
            'and handle args.command == "summary" in main(argv=None). '
            "The resulting Python source must pass compile(content, path, 'exec'). "
            "Preserve concrete source materialization and preserve existing tests; "
            "do not fix implementation work by editing tests only."
        )
        return _SecondRepairReason(
            issue_key="framework_mismatch",
            issue_label="framework mismatch",
            retry_reason="post_repair_framework_mismatch",
            event_reason="post_repair_framework_mismatch_second_pass",
            semantic_violation_code="framework_mismatch",
            step_numbers=[],
            rejection_text=rejection_text,
            cap_used=retry_state.post_repair_framework_second_repair_used,
            cap_attribute="post_repair_framework_second_repair_used",
        )
    return None


def _source_text_is_argparse_cli(source_text: str) -> bool:
    lowered = source_text.lower()
    return (
        "import argparse" in lowered
        and "def build_parser" in source_text
        and "def main(" in source_text
        and ("add_subparsers" in source_text or "add_argument" in source_text)
    )


def _argparse_public_symbols_to_preserve(source_text: str) -> list[str]:
    candidates = [
        "build_parser",
        "build_store",
        "main",
        "TaskStore",
        "format_task_line",
        "format_summary",
    ]
    return [symbol for symbol in candidates if re.search(rf"\b{symbol}\b", source_text)]


def _framework_source_excerpt(source_text: str) -> str:
    lines = []
    for line in source_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(
            token in stripped
            for token in (
                "import argparse",
                "def build_parser",
                "add_subparsers",
                "add_argument",
                "def build_store",
                "TaskStore",
                "format_task_line",
                "format_summary",
                "def main(",
                "args.command",
            )
        ):
            lines.append(stripped)
        if len(lines) >= 14:
            break
    return " ".join(lines)[:900]


def _line_numbered_source_excerpt(
    source_text: str,
    *,
    error_line: Any,
    max_chars: int = 2400,
    context_lines: int = 8,
) -> str:
    if not source_text:
        return ""
    lines = source_text.splitlines()
    if not lines:
        return ""
    try:
        line_number = int(error_line)
    except (TypeError, ValueError):
        line_number = 1
    line_number = max(1, min(line_number, len(lines)))

    candidate = "\n".join(
        f"{index:>4}: {line}" for index, line in enumerate(lines, start=1)
    )
    if len(candidate) <= max_chars:
        return candidate

    start = max(1, line_number - context_lines)
    end = min(len(lines), line_number + context_lines)
    while start < end:
        window = "\n".join(
            f"{index:>4}: {lines[index - 1]}" for index in range(start, end + 1)
        )
        if len(window) <= max_chars:
            prefix = "... preceding lines omitted ...\n" if start > 1 else ""
            suffix = "\n... following lines omitted ..." if end < len(lines) else ""
            return f"{prefix}{window}{suffix}"
        if end - line_number >= line_number - start and end > line_number:
            end -= 1
        elif start < line_number:
            start += 1
        else:
            break
    return "\n".join(
        f"{index:>4}: {lines[index - 1]}"
        for index in range(line_number, line_number + 1)
    )[:max_chars]


def _post_repair_python_source_syntax_reason(
    retry_state: _PlanningRetryState,
    plan_verdict: Any | None,
) -> _SecondRepairReason | None:
    if not plan_verdict:
        return None
    details = getattr(plan_verdict, "details", None) or {}
    issues = [
        issue
        for issue in (details.get("python_source_syntax_invalid") or [])
        if isinstance(issue, dict) and str(issue.get("path") or "").strip()
    ]
    if not issues:
        return None

    first_issue = issues[0]
    path = str(first_issue.get("path") or "").strip()
    line = first_issue.get("line")
    offset = first_issue.get("offset")
    message = str(first_issue.get("message") or "invalid Python syntax").strip()
    raw_candidate = str(first_issue.get("candidate_content") or "")
    line_numbered_excerpt = _line_numbered_source_excerpt(
        raw_candidate,
        error_line=line,
    )
    compact_excerpt = " ".join(
        str(first_issue.get("candidate_content_excerpt") or "").split()
    )[:500]
    location = ""
    if line is not None:
        location = f" line {line}"
        if offset is not None:
            location += f", offset {offset}"
    if line_numbered_excerpt:
        excerpt_clause = (
            " Candidate source excerpt with real newlines preserved:\n"
            f"{line_numbered_excerpt}\n"
            "End candidate source excerpt."
        )
        if first_issue.get("candidate_content_truncated"):
            excerpt_clause += " Candidate content was truncated before prompting."
    else:
        excerpt_clause = (
            f" Candidate content excerpt: {compact_excerpt}" if compact_excerpt else ""
        )
    rejection_text = (
        "python_source_syntax_invalid: repaired Python source is still invalid. "
        f"Affected file: {path}{location}. Syntax error: {message}."
        f"{excerpt_clause} Return a valid JSON array only. Use canonical "
        "`ops.write_file` with complete valid Python source content or "
        "`ops.replace_in_file` with exact current text. write_file.content must "
        "be a JSON string that decodes to real newline characters; do not emit "
        "broken triple-quoted source or literal source-level \\n artifacts. "
        "The resulting Python source must pass compile(content, path, 'exec'). "
        "Preserve concrete source materialization and preserve existing tests; "
        "do not fix implementation work by editing tests only."
    )

    return _SecondRepairReason(
        issue_key="python_source_syntax_invalid",
        issue_label="Python source syntax",
        retry_reason="post_repair_python_source_syntax_invalid",
        event_reason="post_repair_python_source_syntax_second_pass",
        semantic_violation_code="python_source_syntax_invalid",
        step_numbers=[],
        rejection_text=rejection_text,
        cap_used=retry_state.post_repair_python_source_syntax_second_repair_used,
        cap_attribute="post_repair_python_source_syntax_second_repair_used",
    )


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
    planning_root_cause: str | None = None,
) -> bool:
    root_cause = _normalize_planning_root_cause(planning_root_cause)
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
            "planning_root_cause=%s handle_task_failure_called=False "
            "knowledge_recorded=False error=%s",
            ctx.session_id,
            ctx.task_id,
            failure_type,
            root_cause,
            knowledge_exc,
        )
        return False

    ctx.logger.warning(
        "[ORCHESTRATION] session_id=%s task_id=%s failure_type=%s "
        "planning_root_cause=%s handle_task_failure_called=False "
        "knowledge_recorded=%s",
        ctx.session_id,
        ctx.task_id,
        failure_type,
        root_cause,
        knowledge_recorded,
    )
    return knowledge_recorded


def _finalize_planning_timeout_failure(
    *,
    ctx: OrchestrationRunContext,
    failure_type: str,
    failure_reason: str,
    planning_root_cause: str | None = None,
) -> bool:
    return _finalize_planning_terminal_failure(
        ctx=ctx,
        failure_type=failure_type,
        failure_reason=failure_reason,
        generate_failure_summary=True,
        planning_root_cause=planning_root_cause,
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
