"""Planning-phase orchestration flow."""

from __future__ import annotations

import asyncio
import json
import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Dict

from celery.exceptions import SoftTimeLimitExceeded

from app.models import TaskExecution, TaskStatus
from app.schemas.knowledge import KnowledgeContext
from app.services.orchestration.context.assembly import (
    assemble_planning_prompt,
    compress_orchestration_context,
)
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.events.telemetry import emit_phase_event
from app.services.orchestration.state.persistence import (
    append_orchestration_event,
    maybe_emit_divergence_detected,
    record_validation_verdict,
    write_orchestration_state_snapshot,
)
from app.services.orchestration.planning.planner import (
    PlannerService,
    PlanningRepairBudgetExceeded,
    PlanningRepairOutputContractViolation,
)
from app.services.orchestration.planning.normalization import (
    complete_repaired_plan_contract,
    normalize_existing_static_site_plan,
)
from app.services.orchestration.policy import clamp_planning_timeout
from app.services.orchestration.run_state import mark_task_attempt_failed
from app.services.orchestration.state.session_state import mark_session_paused
from app.services.orchestration.task_rules import get_workflow_profile
from app.services.orchestration.workflow_profiles import get_workflow_phases
from app.services.orchestration.types import OrchestrationRunContext
from app.services.orchestration.validation.parsing import (
    extract_plan_steps_from_summary_text,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.services.orchestration.validation.workspace_guard import (
    TaskOperationContractViolation,
)
from app.services.prompt_templates import OrchestrationStatus, estimate_token_count


def _python_exists_verification_command(paths: list[str]) -> str:
    encoded_paths = json.dumps(paths)
    script = (
        "import pathlib,sys; "
        f"files={encoded_paths}; "
        "sys.exit(0 if all(pathlib.Path(p).exists() for p in files) else 1)"
    )
    return "python -c " + json.dumps(script)


def _python_file_contains_verification_command(path: str, needle: str) -> str:
    script = (
        "import pathlib,sys; "
        f"content=pathlib.Path({json.dumps(path)}).read_text(); "
        f"sys.exit(0 if {json.dumps(needle)} in content else 1)"
    )
    return "python -c " + json.dumps(script)


def _grep_quiet_verification_target(command: str) -> tuple[str, str] | None:
    try:
        tokens = shlex.split(str(command or ""), posix=True)
    except ValueError:
        return None
    if len(tokens) < 4 or tokens[0] != "grep" or "-q" not in tokens:
        return None
    quiet_index = tokens.index("-q")
    if quiet_index + 2 >= len(tokens):
        return None
    needle = tokens[quiet_index + 1]
    path = tokens[quiet_index + 2]
    if not needle or not path or path.startswith("-"):
        return None
    return path.lstrip("./"), needle


def _commands_are_weak_expected_file_verification(commands: Any) -> bool:
    if not isinstance(commands, list) or not commands:
        return False
    normalized_commands = [
        str(command or "").strip() for command in commands if str(command or "").strip()
    ]
    if len(normalized_commands) != len(commands):
        return False
    return all(
        ValidatorService._verification_is_weak(command)
        or " ".join(command.split()).startswith(("grep ", "test "))
        for command in normalized_commands
    )


def _strengthen_weak_expected_file_verifications(
    plan: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    strengthened: list[dict[str, Any]] = []
    for step in plan:
        updated = dict(step)
        expected_files = [
            str(path or "").strip().lstrip("./")
            for path in (updated.get("expected_files") or [])
            if str(path or "").strip()
        ]
        grep_target = None
        if expected_files and ValidatorService._verification_is_weak(
            updated.get("verification")
        ):
            grep_target = _grep_quiet_verification_target(
                str(updated.get("verification") or "")
            )
            if grep_target and grep_target[0] in expected_files:
                updated["verification"] = _python_file_contains_verification_command(
                    grep_target[0],
                    grep_target[1],
                )
        if (
            expected_files
            and _commands_are_weak_expected_file_verification(updated.get("commands"))
            and grep_target
        ):
            updated["commands"] = [str(updated.get("verification") or "").strip()]
        strengthened.append(updated)
    return strengthened


def _read_only_stage_fallback_plan(
    ctx: OrchestrationRunContext,
) -> list[dict[str, Any]] | None:
    if ctx.workflow_stage not in {"diagnose", "plan", "review", "validate", "complete"}:
        return None

    script = (
        "import pathlib; "
        "files=[p for p in pathlib.Path('.').rglob('*') "
        "if p.is_file() and '.openclaw' not in p.parts]; "
        "print('\\n'.join(str(p) for p in files[:200]))"
    )
    command = "python -c " + json.dumps(script)
    stage_label = str(ctx.workflow_stage or "review").replace("_", " ")
    return [
        {
            "step_number": 1,
            "description": f"Inspect workspace for {stage_label} stage",
            "commands": [command],
            "verification": command,
            "rollback": "true",
            "expected_files": [],
        }
    ]


def _static_site_validation_fallback_plan(
    ctx: OrchestrationRunContext,
) -> list[dict[str, Any]] | None:
    if ctx.workflow_stage != "validate":
        return None

    prompt = str(ctx.prompt or "")
    lowered = prompt.lower()
    project_dir = Path(ctx.orchestration_state.project_dir)
    root = "public/status-site" if "public/status-site" in lowered else ""
    if not root:
        public_dir = project_dir / "public"
        if not public_dir.is_dir():
            return None
        for child in sorted(public_dir.iterdir()):
            if not child.is_dir():
                continue
            if (child / "index.html").is_file() and (
                child / "css" / "style.css"
            ).is_file():
                root = f"public/{child.name}"
                break
    if not root:
        return None

    files = [
        f"{root}/index.html",
        f"{root}/css/style.css",
        f"{root}/images/status-badge.svg",
    ]
    needles = ["css/style.css", "images/status-badge.svg"]
    for label in ("API", "Queue", "Knowledge"):
        if label.lower() in lowered:
            needles.append(label)
    if "skip link" in lowered or "skip-link" in lowered:
        needles.append("skip")
    if "alt text" in lowered or "alt=" in lowered:
        needles.append("alt=")

    script = (
        "import pathlib,sys; "
        f"files={json.dumps(files)}; "
        f"index={json.dumps(files[0])}; "
        f"needles={json.dumps(list(dict.fromkeys(needles)))}; "
        "ok=all(pathlib.Path(p).is_file() and pathlib.Path(p).stat().st_size > 0 for p in files); "
        "content=pathlib.Path(index).read_text(encoding='utf-8') if pathlib.Path(index).is_file() else ''; "
        "ok=ok and all(needle in content for needle in needles); "
        "sys.exit(0 if ok else 1)"
    )
    command = "python -c " + json.dumps(script)
    return [
        {
            "step_number": 1,
            "description": f"Validate static site contract under {root}",
            "commands": [command],
            "verification": command,
            "rollback": "true",
            "expected_files": [],
            "ops": [],
        }
    ]


def _split_repaired_single_step_full_lifecycle_plan(
    extracted_plan: Any,
) -> list[dict[str, Any]] | None:
    if not isinstance(extracted_plan, list) or len(extracted_plan) != 1:
        return None
    original = extracted_plan[0]
    if not isinstance(original, dict):
        return None

    ops = original.get("ops") if isinstance(original.get("ops"), list) else []
    commands = (
        original.get("commands") if isinstance(original.get("commands"), list) else []
    )
    commands = [
        str(command or "").strip() for command in commands if str(command or "").strip()
    ]
    expected_files = (
        original.get("expected_files")
        if isinstance(original.get("expected_files"), list)
        else []
    )
    expected_files = [
        str(path or "").strip().lstrip("./")
        for path in expected_files
        if str(path or "").strip()
    ]
    op_paths = [
        str(operation.get("path") or "").strip().lstrip("./")
        for operation in ops
        if isinstance(operation, dict)
        and str(operation.get("op") or "")
        in {"write_file", "append_file", "replace_in_file"}
        and str(operation.get("path") or "").strip()
    ]
    # When splitting a repaired single-step full-lifecycle plan, keep the
    # implementation contract anchored to concrete file edits. Repair models
    # often preserve broad/speculative expected_files from the rejected plan;
    # carrying those into the split causes false unmaterialized-output failures.
    material_paths = list(dict.fromkeys(op_paths or expected_files))
    original_verification = str(original.get("verification") or "").strip()
    verifier = (
        _python_exists_verification_command(material_paths)
        if material_paths
        else original_verification
    )
    if not (ops or commands) or not verifier:
        return None

    implementation_step: dict[str, Any] = {
        "step_number": 2,
        "description": str(original.get("description") or "Apply requested change"),
        "commands": commands,
        "verification": verifier,
        "rollback": original.get("rollback"),
        "expected_files": material_paths,
    }
    if ops:
        implementation_step["ops"] = ops

    return [
        {
            "step_number": 1,
            "description": "Inspect the current workspace",
            "commands": ["rg --files . | sort"],
            "verification": 'python -c "import sys; sys.exit(0)"',
            "rollback": None,
            "expected_files": [],
        },
        implementation_step,
        {
            "step_number": 3,
            "description": "Verify the requested change",
            "commands": [verifier],
            "verification": verifier,
            "rollback": None,
            "expected_files": [],
        },
    ]


def _prune_unmaterialized_expected_files(
    plan: list[dict[str, Any]],
    unmaterialized_paths: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Drop expected_files entries that validation proved are not outputs.

    This is deliberately narrower than contract completion. It never adds file
    content or paths; it only removes stale/speculative expected_files while
    preserving concrete mutating op targets as the implementation scope.
    """

    if not unmaterialized_paths:
        return plan, {"changed": False, "reason": "no_unmaterialized_expected_files"}

    stale_paths = {
        str(path or "").strip().rstrip("/").lstrip("./")
        for path in unmaterialized_paths
        if str(path or "").strip()
    }
    if not stale_paths:
        return plan, {"changed": False, "reason": "empty_unmaterialized_expected_files"}

    concrete_op_paths = {
        str(op.get("path") or "").strip().rstrip("/").lstrip("./")
        for step in plan
        if isinstance(step, dict)
        for op in (step.get("ops") or [])
        if isinstance(op, dict)
        and str(op.get("op") or "") in {"write_file", "append_file", "replace_in_file"}
        and str(op.get("path") or "").strip()
    }
    if not concrete_op_paths:
        return plan, {"changed": False, "reason": "no_concrete_file_ops"}

    changed = False
    removed: list[str] = []
    normalized: list[dict[str, Any]] = []
    for step in plan:
        if not isinstance(step, dict):
            normalized.append(step)
            continue
        updated = dict(step)
        expected_files = []
        for raw_path in updated.get("expected_files") or []:
            path = str(raw_path or "").strip().rstrip("/").lstrip("./")
            if not path:
                continue
            if path in stale_paths and path not in concrete_op_paths:
                removed.append(path)
                changed = True
                continue
            expected_files.append(path)
        updated["expected_files"] = list(dict.fromkeys(expected_files))
        normalized.append(updated)

    return normalized, {
        "changed": changed,
        "reason": (
            "pruned_unmaterialized_expected_files"
            if changed
            else "no_speculative_expected_files_removed"
        ),
        "removed_expected_files": sorted(set(removed)),
        "concrete_op_paths": sorted(concrete_op_paths),
    }


def _retrieve_validation_repair_knowledge(
    ctx: OrchestrationRunContext,
    *,
    query: str,
    failure_signature: str | None = None,
) -> KnowledgeContext | None:
    knowledge_ctx = _retrieve_knowledge(
        ctx,
        trigger_phase="validation",
        knowledge_types=[
            "failure_memory",
            "format_guide",
            "debug_case",
        ],
        query=query,
        failure_signature=failure_signature,
    )
    if knowledge_ctx:
        _log_knowledge_usage(ctx, knowledge_ctx, used_in_prompt=True)
    return knowledge_ctx


def _looks_like_verification_only_task(
    title: str | None,
    description: str | None,
) -> bool:
    combined = f"{title or ''}\n{description or ''}".lower()
    verification_markers = (
        "verification command",
        "verification commands",
        "improve task verification",
        "strengthen verification",
        "checks prove",
        "content-aware checks",
        "file/content checks",
        "audit",
        "inspect current files",
        "no major implementation",
        "do not change page design",
        "do not change page design much",
    )
    if not any(marker in combined for marker in verification_markers):
        return False
    implementation_markers = (
        "create `",
        "create ",
        "add new ",
        "add a new ",
        "add seasonal",
        "add second",
        "adjust one content block",
        "adjust one css",
        "update css",
        "edit `",
        "edits `",
        "start task that edits",
    )
    return not any(marker in combined for marker in implementation_markers)


# Circuit breaker: abort planning after this many consecutive validation failures
# to prevent infinite retry loops that hang the session.
from app.services.orchestration.phases.planning_support import (
    MAX_PLANNING_RETRIES,
    TRUNCATED_PLAN_REPAIR_REJECTION_REASON,
    _PlanningRetryState,
    _build_reasoning_artifact,
    _build_repair_rejection_reasons,
    _classify_planning_timeout_failure,
    _compress_project_context_for_planning,
    _emit_planning_diagnostics_contract_violation,
    _get_targeted_second_repair_reason,
    _is_repairable_malformed_shell_quoting_violation,
    _plan_contract_diagnostics,
    _semantic_codes_for_immediate_repair_issues,
    _should_repair_truncated_single_step_plan,
    _terminal_validation_failure_details,
    _truncated_multistep_collapse_diagnostics,
)


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
    """Extract a truncated text snippet from the last planning result for operator context."""
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
    """Count failed TaskExecution rows for this task/session before current execution.

    Used to seed _PlanningRetryState.persisted_failures so the circuit breaker
    survives worker restarts — F12 (stateless reducer).
    """
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


def execute_planning_phase(
    *,
    ctx: OrchestrationRunContext,
    workspace_review: Dict[str, Any],
    extract_structured_text: Callable[[Any], str],
    extract_plan_steps: Callable[[Any], Any],
    looks_like_truncated_multistep_plan: Callable[[str, Any], bool],
    normalize_plan_with_live_logging: Callable[..., Any],
    workspace_violation_error_cls: type[Exception],
) -> Dict[str, Any]:
    ctx.logger.info("[ORCHESTRATION] Phase 1: PLANNING - generating step plan")
    emit_phase_event(
        ctx.orchestration_state,
        ctx.emit_live,
        level="INFO",
        phase="planning",
        message="[ORCHESTRATION] Phase 1: PLANNING - generating step plan",
        details={
            "project_context_chars": len(ctx.orchestration_state.project_context or ""),
            "task_chars": len(ctx.prompt or ""),
        },
    )
    planning_phase_event = None
    try:
        planning_phase_event = append_orchestration_event(
            project_dir=ctx.orchestration_state.project_dir,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            event_type=EventType.PHASE_STARTED,
            details={"phase": "planning"},
        )
        write_orchestration_state_snapshot(
            project_dir=ctx.orchestration_state.project_dir,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            orchestration_state=ctx.orchestration_state,
            trigger="phase_started",
            related_event_id=planning_phase_event.get("event_id"),
        )
    except Exception as exc:
        ctx.logger.debug(
            "[ORCHESTRATION] Failed to persist planning phase start event/snapshot: %s",
            exc,
        )

    ctx.workflow_profile = get_workflow_profile(
        ctx.execution_profile,
        getattr(ctx.task, "title", None),
        getattr(ctx.task, "description", None),
    )
    # Template workflow_profile overrides the heuristic-derived value.
    _tmpl_id = getattr(ctx.task, "template_id", None)
    if _tmpl_id:
        try:
            from app.services.orchestration.workflow_templates import (
                get_template as _gt,
            )

            _tmpl = _gt(_tmpl_id)
            if _tmpl:
                ctx.workflow_profile = _tmpl.workflow_profile
        except Exception:
            pass
    ctx.workflow_phases = get_workflow_phases(ctx.workflow_profile)
    ctx.workspace_has_existing_files = bool(workspace_review.get("has_existing_files"))
    if len(ctx.orchestration_state.project_context or "") > 3500:
        compressed_context = _compress_project_context_for_planning(
            ctx.orchestration_state
        )
        if compressed_context != (ctx.orchestration_state.project_context or ""):
            ctx.orchestration_state.project_context = compressed_context
            ctx.logger.info(
                "[ORCHESTRATION] Compressed oversized planning context before first prompt (%d chars)",
                len(compressed_context),
            )
    planning_knowledge_ctx = _retrieve_knowledge(
        ctx, trigger_phase="planning", knowledge_types=["format_guide", "task_example"]
    )
    planning_prompt = (
        assemble_planning_prompt(
            ctx,
            workspace_review,
            knowledge_context=(
                planning_knowledge_ctx
                if planning_knowledge_ctx and planning_knowledge_ctx.retrieved_items
                else None
            ),
        )
        if ctx.runtime_service
        else None
    )
    runtime_metadata = (
        ctx.runtime_service.get_backend_metadata()
        if ctx.runtime_service and hasattr(ctx.runtime_service, "get_backend_metadata")
        else {}
    )
    prompt_profile = PlannerService.select_prompt_profile(
        runtime_metadata.get("backend"),
        runtime_metadata.get("model_family"),
    )
    model_capability_label = PlannerService.model_capability_label(
        runtime_metadata.get("backend"),
        runtime_metadata.get("model_family"),
    )
    if planning_prompt:
        planning_prompt = PlannerService.apply_prompt_profile(
            planning_prompt,
            prompt_profile=prompt_profile,
        )
    if planning_knowledge_ctx:
        _log_knowledge_usage(
            ctx, planning_knowledge_ctx, used_in_prompt=bool(planning_prompt)
        )
    planning_prompt_tokens = estimate_token_count(planning_prompt or "")
    # 10H-C: emit context budget on every planning attempt so it appears in
    # session debug output regardless of dense-prompt detection.
    emit_phase_event(
        ctx.orchestration_state,
        ctx.emit_live,
        level="INFO",
        phase="planning",
        message=(
            f"[ORCHESTRATION] Planning context budget: "
            f"~{planning_prompt_tokens} tokens "
            f"(project_context={len(ctx.orchestration_state.project_context or '')}c "
            f"task={len(ctx.prompt or '')}c)"
        ),
        details={
            "planning_prompt_tokens": planning_prompt_tokens,
            "project_context_chars": len(ctx.orchestration_state.project_context or ""),
            "task_chars": len(ctx.prompt or ""),
            "prompt_profile": prompt_profile,
            "model_capability_label": model_capability_label,
            "context_budget_status": (
                "dense" if planning_prompt_tokens > 8000 else "normal"
            ),
        },
    )

    planning_timeout_seconds = clamp_planning_timeout(ctx.timeout_seconds)
    start_with_minimal_planning_prompt = (
        PlannerService.should_start_with_minimal_prompt(
            ctx.prompt,
            ctx.orchestration_state.project_context,
        )
    )
    if workspace_review.get("has_existing_files"):
        start_with_minimal_planning_prompt = True
    # Qwen3.5-35B has a 128 k context window; 2200 tokens is far too
    # conservative.  Use 8000 tokens (~6000 words) as the boundary
    # so normal project contexts get the full planning prompt.
    MINIMAL_PROMPT_TOKEN_THRESHOLD = 8000
    if planning_prompt_tokens > MINIMAL_PROMPT_TOKEN_THRESHOLD:
        start_with_minimal_planning_prompt = True
        # Always compress project_context when the prompt is dense.
        # Later tasks in a session accumulate workspace files that inflate the
        # project_context even when no debug/completed steps exist, causing
        # dense_planning_context failures for beta-python and gamma-docs tasks.
        _compressed = _compress_project_context_for_planning(ctx.orchestration_state)
        if _compressed:
            ctx.orchestration_state.project_context = _compressed
            ctx.logger.info(
                "[ORCHESTRATION] Context compressed for dense planning (%d chars)",
                len(_compressed),
            )
    used_minimal_planning_prompt = start_with_minimal_planning_prompt

    if start_with_minimal_planning_prompt:
        emit_phase_event(
            ctx.orchestration_state,
            ctx.emit_live,
            level="WARN",
            phase="planning",
            message="[ORCHESTRATION] Planning context is dense; starting with minimal prompt",
            details={
                "strategy": "minimal_prompt_first",
                "project_context_length": len(
                    ctx.orchestration_state.project_context or ""
                ),
                "estimated_prompt_tokens": planning_prompt_tokens,
            },
        )
        planning_result = PlannerService.retry_with_minimal_prompt(
            runtime_service=ctx.runtime_service,
            task_description=ctx.prompt,
            project_dir=ctx.orchestration_state.project_dir,
            timeout_seconds=planning_timeout_seconds,
            logger=ctx.logger,
            emit_live=ctx.emit_live,
            reason="dense_planning_context",
            prompt_profile=prompt_profile,
            workflow_profile=ctx.workflow_profile,
            workflow_phases=getattr(ctx, "workflow_phases", []),
            workspace_has_existing_files=getattr(
                ctx, "workspace_has_existing_files", False
            ),
            knowledge_context=(
                planning_knowledge_ctx
                if planning_knowledge_ctx and planning_knowledge_ctx.retrieved_items
                else None
            ),
        )
    else:
        planning_result = asyncio.run(
            PlannerService._execute_task_with_planning_lock(
                ctx.runtime_service,
                planning_prompt,
                timeout_seconds=planning_timeout_seconds,
                reuse_task_session=False,
                diagnostic_label="PLANNING",
                diagnostic_metadata={
                    "session_id": ctx.session_id,
                    "task_id": ctx.task_id,
                    "task_execution_id": ctx.task_execution_id,
                    "workflow_profile": ctx.workflow_profile,
                    "prompt_profile": prompt_profile,
                    "planning_prompt_tokens": planning_prompt_tokens,
                    "planning_attempt": "initial",
                },
            )
        )

    emit_phase_event(
        ctx.orchestration_state,
        ctx.emit_live,
        level="INFO",
        phase="planning",
        message="[ORCHESTRATION] Planning response received; parsing and validating plan",
        details={"phase_state": "planning_response_received"},
    )

    initial_output_text = __coerce_output_text(
        ctx=ctx,
        planning_result=planning_result,
        output_result=planning_result.get("output", ""),
        extract_structured_text=extract_structured_text,
    )
    if PlannerService.should_retry_with_minimal_prompt(
        planning_result, initial_output_text
    ) and not PlannerService.looks_salvageable_planning_output(initial_output_text):
        if used_minimal_planning_prompt:
            if PlannerService.is_openclaw_lock_contention(planning_result):
                failure_type = "planning_openclaw_lock_contention"
                failure_reason = (
                    "OpenClaw planning failed because the local session file was locked"
                )
                restore_reason = "planning OpenClaw lock contention"
            else:
                failure_type = "planning_timeout"
                failure_reason = (
                    "Planning timed out or exceeded context after "
                    f"{planning_timeout_seconds}s"
                )
                restore_reason = "planning timeout or context overflow"
            ctx.logger.error(
                "[ORCHESTRATION] Planning timed out before a salvageable response was produced: %s",
                failure_reason,
            )
            ctx.orchestration_state.status = OrchestrationStatus.ABORTED
            ctx.orchestration_state.abort_reason = failure_reason
            emit_phase_event(
                ctx.orchestration_state,
                ctx.emit_live,
                level="ERROR",
                phase="planning",
                message=(
                    "[ORCHESTRATION] Planning terminalized before a valid plan "
                    f"was produced: {failure_reason}"
                ),
                details={"reason": failure_type},
            )
            _finalize_planning_timeout_failure(
                ctx=ctx,
                failure_type=failure_type,
                failure_reason=failure_reason,
            )
            if ctx.restore_workspace_snapshot_if_needed:
                ctx.restore_workspace_snapshot_if_needed(restore_reason)
            return {"status": "failed", "reason": failure_type}
        ctx.logger.warning(
            "[ORCHESTRATION] Planning failed on the first pass; retrying with minimal prompt"
        )
        emit_phase_event(
            ctx.orchestration_state,
            ctx.emit_live,
            level="WARN",
            phase="planning",
            message="[ORCHESTRATION] Planning needed a fallback; retrying with minimal prompt",
            details={
                "retry": "minimal_prompt",
                "reason": (planning_result.get("error") or initial_output_text)[:240],
            },
        )
        planning_result = PlannerService.retry_with_minimal_prompt(
            runtime_service=ctx.runtime_service,
            task_description=ctx.prompt,
            project_dir=ctx.orchestration_state.project_dir,
            timeout_seconds=planning_timeout_seconds,
            logger=ctx.logger,
            emit_live=ctx.emit_live,
            reason=(planning_result.get("error") or initial_output_text),
            prompt_profile=prompt_profile,
            workflow_profile=ctx.workflow_profile,
            workflow_phases=getattr(ctx, "workflow_phases", []),
            workspace_has_existing_files=getattr(
                ctx, "workspace_has_existing_files", False
            ),
            knowledge_context=(
                planning_knowledge_ctx
                if planning_knowledge_ctx and planning_knowledge_ctx.retrieved_items
                else None
            ),
        )
        used_minimal_planning_prompt = True

    persisted_failures = _count_prior_failed_planning_executions(ctx)
    retry_state = _PlanningRetryState(persisted_failures=persisted_failures)
    retry_state.minimal_prompt_used = used_minimal_planning_prompt
    if persisted_failures > 0:
        ctx.logger.warning(
            "[ORCHESTRATION] session_id=%s task_id=%s "
            "persisted_planning_failures=%d — circuit breaker seeded from DB",
            ctx.session_id,
            ctx.task_id,
            persisted_failures,
        )
    try:
        while True:
            # Circuit breaker: abort after too many consecutive validation failures.
            # Combined count includes persisted failures from prior executions so
            # a worker restart cannot reset the counter to zero (F12).
            if retry_state.circuit_open:
                has_persisted = retry_state.persisted_failures > 0
                cb_reason = (
                    "planning_circuit_breaker_opened_persisted_attempts"
                    if has_persisted
                    else "planning_circuit_breaker_opened"
                )
                total_failures = (
                    retry_state.consecutive_failures + retry_state.persisted_failures
                )
                ctx.orchestration_state.status = OrchestrationStatus.ABORTED
                ctx.orchestration_state.abort_reason = (
                    f"Planning failed {total_failures} time(s) "
                    f"({retry_state.persisted_failures} prior + "
                    f"{retry_state.consecutive_failures} this run); "
                    "circuit breaker opened to prevent infinite retry loop"
                )
                emit_phase_event(
                    ctx.orchestration_state,
                    ctx.emit_live,
                    level="ERROR",
                    phase="planning",
                    message=(
                        f"[ORCHESTRATION] Planning circuit breaker opened after "
                        f"{total_failures} failure(s) "
                        f"({retry_state.persisted_failures} persisted + "
                        f"{retry_state.consecutive_failures} in-session)"
                    ),
                    details={
                        "reason": cb_reason,
                        "persisted_failures": retry_state.persisted_failures,
                        "consecutive_failures": retry_state.consecutive_failures,
                        "total_failures": total_failures,
                    },
                )
                last_snippet = _last_plan_output_snippet(planning_result)
                last_repair = retry_state.last_repair_reason or "none"
                cb_failure_reason = (
                    f"Planning failed {total_failures} time(s) "
                    f"({retry_state.persisted_failures} prior + "
                    f"{retry_state.consecutive_failures} this run). "
                    f"Last repair reason: {last_repair}. "
                    + (
                        f"Last plan output: {last_snippet}"
                        if last_snippet
                        else "No plan output was produced."
                    )
                )
                _finalize_planning_terminal_failure(
                    ctx=ctx,
                    failure_type=cb_reason,
                    failure_reason=cb_failure_reason,
                    generate_failure_summary=True,
                )
                if ctx.restore_workspace_snapshot_if_needed:
                    ctx.restore_workspace_snapshot_if_needed(
                        "planning circuit breaker opened"
                    )
                return {"status": "failed", "reason": cb_reason}

            output_result = planning_result.get("output", {})
            ctx.logger.info(
                "[ORCHESTRATION] Attempting JSON parse (failure_count=%d)",
                retry_state.consecutive_failures,
            )
            output_text = __coerce_output_text(
                ctx=ctx,
                planning_result=planning_result,
                output_result=output_result,
                extract_structured_text=extract_structured_text,
            )

            success, plan_data, strategy_info = ctx.error_handler.attempt_json_parsing(
                output_text, context="planning"
            )
            if not success:
                ctx.logger.warning(
                    "[ORCHESTRATION] JSON parse failed: %s", strategy_info
                )
                extracted_summary_plan = extract_plan_steps_from_summary_text(
                    output_text
                )
                if extracted_summary_plan is not None:
                    strategy_info = (
                        "planning_contract_violation: multi_step_prose_summary"
                    )
                    ctx.logger.warning(
                        "[ORCHESTRATION] Planning output was multi-step prose instead of JSON; routing to fallback/repair"
                    )
                workspace_plan = PlannerService.maybe_load_workspace_plan(
                    output_text=output_text,
                    project_dir=ctx.orchestration_state.project_dir,
                    logger=ctx.logger,
                )
                if workspace_plan is not None:
                    success = True
                    plan_data = workspace_plan
                    strategy_info = "Recovered plan from workspace plan.json"
                    ctx.logger.info(
                        "[ORCHESTRATION] Planning output referenced plan.json; using workspace file instead of strict JSON retry"
                    )

            if (
                PlannerService.should_retry_with_minimal_prompt(
                    planning_result, output_text
                )
                and not success
                and not PlannerService.looks_salvageable_planning_output(output_text)
            ):
                if PlannerService.is_openclaw_lock_contention(planning_result):
                    raise TimeoutError(
                        "OpenClaw planning failed because the local session file "
                        "was locked"
                    )
                raise TimeoutError(
                    f"Planning timed out or exceeded context after {planning_timeout_seconds}s"
                )

            if not success and not retry_state.minimal_prompt_used:
                contract_violations = (
                    PlannerService.describe_planning_contract_violations(
                        output_text=output_text,
                        parse_success=False,
                        strategy_info=strategy_info,
                    )
                )
                ctx.logger.warning(
                    "[ORCHESTRATION] Planning contract violation before minimal retry: %s",
                    "; ".join(contract_violations),
                )
                _emit_planning_diagnostics_contract_violation(
                    ctx,
                    reason="json_parse_failed_before_minimal",
                    contract_violations=contract_violations,
                    output_text=output_text,
                    strategy_info=strategy_info,
                )
                ctx.logger.info(
                    "[ORCHESTRATION] JSON parse failed, switching to minimal prompt"
                )
                planning_result = __retry_with_minimal_prompt(
                    ctx=ctx,
                    planning_timeout_seconds=planning_timeout_seconds,
                    reason=f"json_parse_failed: {output_text[:240]}",
                    prompt_profile=prompt_profile,
                    knowledge_context=planning_knowledge_ctx,
                )
                retry_state.minimal_prompt_used = True
                retry_state.consecutive_failures += 1
                continue

            if not success and not retry_state.repair_prompt_used:
                contract_violations = (
                    PlannerService.describe_planning_contract_violations(
                        output_text=output_text,
                        parse_success=False,
                        strategy_info=strategy_info,
                    )
                )
                emit_phase_event(
                    ctx.orchestration_state,
                    ctx.emit_live,
                    level="WARN",
                    phase="planning",
                    message="[ORCHESTRATION] Planning response was malformed or truncated; starting repair pass",
                    details={
                        "reason": f"json_parse_failed_after_minimal: {strategy_info}"[
                            :240
                        ],
                        "output_chars": len(output_text or ""),
                        "contract_violations": contract_violations[:8],
                    },
                )
                ctx.logger.warning(
                    "[ORCHESTRATION] Planning contract violation before repair: %s",
                    "; ".join(contract_violations),
                )
                _emit_planning_diagnostics_contract_violation(
                    ctx,
                    reason="json_parse_failed_after_minimal",
                    contract_violations=contract_violations,
                    output_text=output_text,
                    strategy_info=strategy_info,
                )
                ctx.logger.info(
                    "[ORCHESTRATION] Calling repair pass for planning output"
                )
                retry_state.last_repair_reason = (
                    f"json_parse_failed_after_minimal: {strategy_info}"
                )
                planning_result = __repair_planning_output(
                    ctx=ctx,
                    planning_timeout_seconds=planning_timeout_seconds,
                    malformed_output=output_text,
                    reason=f"json_parse_failed_after_minimal: {strategy_info}",
                    prompt_profile=prompt_profile,
                )
                retry_state.repair_prompt_used = True
                retry_state.consecutive_failures += 1
                continue

            if not success:
                ctx.orchestration_state.status = OrchestrationStatus.ABORTED
                ctx.orchestration_state.abort_reason = (
                    f"Planning JSON parse failed: {strategy_info}"
                )
                emit_phase_event(
                    ctx.orchestration_state,
                    ctx.emit_live,
                    level="ERROR",
                    phase="planning",
                    message=f"[ORCHESTRATION] Planning JSON parse failed: {strategy_info}",
                    details={"reason": "planning_json_error"},
                )
                _finalize_planning_terminal_failure(
                    ctx=ctx,
                    failure_type="planning_json_error",
                    failure_reason=(
                        f"Planning JSON parse failed: {strategy_info}. "
                        f"Raw output: {output_text[:500]}"
                    ),
                )
                if ctx.restore_workspace_snapshot_if_needed:
                    ctx.restore_workspace_snapshot_if_needed(
                        "planning JSON parse failure"
                    )
                return {"status": "failed", "reason": "planning_json_error"}

            extracted_plan = extract_plan_steps(plan_data)
            if extracted_plan is None and not retry_state.minimal_prompt_used:
                contract_violations = (
                    PlannerService.describe_planning_contract_violations(
                        output_text=output_text,
                        parse_success=True,
                        strategy_info="unexpected_plan_shape",
                        plan_data=plan_data,
                    )
                )
                ctx.logger.warning(
                    "[ORCHESTRATION] Planning contract violation before minimal retry: %s",
                    "; ".join(contract_violations),
                )
                _emit_planning_diagnostics_contract_violation(
                    ctx,
                    reason="unexpected_plan_shape_before_minimal",
                    contract_violations=contract_violations,
                    output_text=output_text,
                    strategy_info="unexpected_plan_shape",
                )
                ctx.logger.info(
                    "[ORCHESTRATION] Plan extraction failed, switching to minimal prompt"
                )
                planning_result = __retry_with_minimal_prompt(
                    ctx=ctx,
                    planning_timeout_seconds=planning_timeout_seconds,
                    reason=f"unexpected_plan_shape: {str(plan_data)[:240]}",
                    prompt_profile=prompt_profile,
                    knowledge_context=planning_knowledge_ctx,
                )
                retry_state.minimal_prompt_used = True
                retry_state.consecutive_failures += 1
                continue

            if extracted_plan is None and not retry_state.repair_prompt_used:
                contract_violations = (
                    PlannerService.describe_planning_contract_violations(
                        output_text=output_text,
                        parse_success=True,
                        strategy_info="unexpected_plan_shape_after_minimal",
                        plan_data=plan_data,
                    )
                )
                ctx.logger.warning(
                    "[ORCHESTRATION] Planning contract violation before repair: %s",
                    "; ".join(contract_violations),
                )
                _emit_planning_diagnostics_contract_violation(
                    ctx,
                    reason="unexpected_plan_shape_after_minimal",
                    contract_violations=contract_violations,
                    output_text=output_text,
                    strategy_info="unexpected_plan_shape_after_minimal",
                )
                ctx.logger.info(
                    "[ORCHESTRATION] Plan extraction failed, calling repair"
                )
                retry_state.last_repair_reason = "unexpected_plan_shape_after_minimal"
                planning_result = __repair_planning_output(
                    ctx=ctx,
                    planning_timeout_seconds=planning_timeout_seconds,
                    malformed_output=output_text,
                    reason="unexpected_plan_shape_after_minimal",
                    prompt_profile=prompt_profile,
                )
                retry_state.repair_prompt_used = True
                retry_state.consecutive_failures += 1
                continue

            repair_shrank_multistep_plan = False
            single_step_full_lifecycle_plan = False
            verification_only_task = _looks_like_verification_only_task(
                getattr(ctx.task, "title", None),
                getattr(ctx.task, "description", None),
            )
            if isinstance(extracted_plan, list):
                if len(extracted_plan) > 1:
                    retry_state.last_multistep_plan_step_count = max(
                        retry_state.last_multistep_plan_step_count,
                        len(extracted_plan),
                    )
                elif (
                    len(extracted_plan) == 1
                    and retry_state.repair_prompt_used
                    and retry_state.last_multistep_plan_step_count > 1
                ):
                    repair_shrank_multistep_plan = True
                elif (
                    len(extracted_plan) == 1
                    and ctx.execution_profile == "full_lifecycle"
                    and bool(getattr(ctx, "workspace_has_existing_files", False))
                    and not verification_only_task
                ):
                    single_step_full_lifecycle_plan = True

            if single_step_full_lifecycle_plan and not retry_state.repair_prompt_used:
                # Attempt deterministic lifecycle expansion before consuming a repair
                # token. When the original single-step plan already has ops or clear
                # artifact commands (manifest.json, config files, etc.), splitting
                # into inspect/implement/verify deterministically prevents repair from
                # drifting into unrelated solution.py implementations.
                normalized_plan = _split_repaired_single_step_full_lifecycle_plan(
                    extracted_plan
                )
                if normalized_plan:
                    emit_phase_event(
                        ctx.orchestration_state,
                        ctx.emit_live,
                        level="INFO",
                        phase="planning",
                        message=(
                            "[ORCHESTRATION] Normalized original single-step artifact "
                            "plan into deterministic lifecycle steps (skipping repair)"
                        ),
                        details={
                            "reason": "single_step_artifact_plan_normalized",
                            "step_count": len(normalized_plan),
                        },
                    )
                    extracted_plan = normalized_plan
                    output_text = json.dumps(normalized_plan)
                    single_step_full_lifecycle_plan = False
                    # Fall through to validation without consuming a repair attempt
                else:
                    contract_violations = [
                        "Full-lifecycle planning must return separate inspect, implementation, and verification steps"
                    ]
                    _emit_planning_diagnostics_contract_violation(
                        ctx,
                        reason="single_step_full_lifecycle_plan",
                        contract_violations=contract_violations,
                        output_text=output_text,
                        strategy_info="single_step_full_lifecycle_plan",
                    )
                    retry_state.last_repair_reason = "single_step_full_lifecycle_plan"
                    planning_result = __repair_planning_output(
                        ctx=ctx,
                        planning_timeout_seconds=planning_timeout_seconds,
                        malformed_output=output_text,
                        reason="single_step_full_lifecycle_plan",
                        rejection_reasons=[
                            "Return 3 or 4 separate step objects: inspect current workspace, implement the requested change, and verify behavior/content. Do not merge the full task into one step."
                        ],
                        prompt_profile=prompt_profile,
                        knowledge_context=planning_knowledge_ctx,
                    )
                    retry_state.repair_prompt_used = True
                    retry_state.consecutive_failures += 1
                    continue

            if (
                looks_like_truncated_multistep_plan(output_text, extracted_plan)
                and not retry_state.minimal_prompt_used
            ):
                truncated_diagnostics = _truncated_multistep_collapse_diagnostics(
                    output_text=output_text,
                    extracted_plan=extracted_plan,
                    repair_stage=(
                        "after_first_repair"
                        if retry_state.repair_prompt_used
                        else "before_first_repair"
                    ),
                )
                if (
                    _should_repair_truncated_single_step_plan(
                        prompt_profile=prompt_profile,
                        extracted_plan=extracted_plan,
                        execution_profile=ctx.execution_profile,
                    )
                    and not verification_only_task
                ):
                    emit_phase_event(
                        ctx.orchestration_state,
                        ctx.emit_live,
                        level="WARN",
                        phase="planning",
                        message=(
                            "[ORCHESTRATION] Planning output collapsed into a "
                            "single step on the local model path; starting a "
                            "repair pass instead of executing it as-is"
                        ),
                        details={
                            "reason": "truncated_multistep_plan_repair_requested",
                            "model_profile": prompt_profile,
                        },
                    )
                    _emit_planning_diagnostics_contract_violation(
                        ctx,
                        reason="truncated_multistep_plan_detected",
                        contract_violations=[
                            "truncated multi-step plan collapsed into a single step"
                        ],
                        contract_diagnostics=truncated_diagnostics,
                        output_text=output_text,
                        strategy_info="truncated_multistep_plan_repair_requested",
                    )
                    retry_state.last_repair_reason = "truncated_multistep_plan_detected"
                    planning_result = __repair_planning_output(
                        ctx=ctx,
                        planning_timeout_seconds=planning_timeout_seconds,
                        malformed_output=output_text,
                        reason="truncated_multistep_plan_detected",
                        rejection_reasons=_build_repair_rejection_reasons(
                            [TRUNCATED_PLAN_REPAIR_REJECTION_REASON],
                            truncated_diagnostics,
                        ),
                        prompt_profile=prompt_profile,
                    )
                    retry_state.repair_prompt_used = True
                    retry_state.consecutive_failures += 1
                    continue
                else:
                    _emit_planning_diagnostics_contract_violation(
                        ctx,
                        reason="truncated_multistep_plan_detected",
                        contract_violations=[
                            "truncated multi-step plan collapsed into a single step"
                        ],
                        contract_diagnostics=truncated_diagnostics,
                        output_text=output_text,
                        strategy_info="truncated_multistep_plan_minimal_retry",
                    )
                    planning_result = __retry_with_minimal_prompt(
                        ctx=ctx,
                        planning_timeout_seconds=planning_timeout_seconds,
                        reason="truncated_multistep_plan_detected",
                        prompt_profile=prompt_profile,
                        knowledge_context=planning_knowledge_ctx,
                    )
                    retry_state.minimal_prompt_used = True
                    retry_state.consecutive_failures += 1
                    continue

            if (
                looks_like_truncated_multistep_plan(output_text, extracted_plan)
                and not retry_state.repair_prompt_used
            ):
                truncated_diagnostics = _truncated_multistep_collapse_diagnostics(
                    output_text=output_text,
                    extracted_plan=extracted_plan,
                    repair_stage="before_first_repair",
                )
                _emit_planning_diagnostics_contract_violation(
                    ctx,
                    reason="truncated_multistep_plan_after_minimal",
                    contract_violations=[
                        "truncated multi-step plan collapsed into a single step"
                    ],
                    contract_diagnostics=truncated_diagnostics,
                    output_text=output_text,
                    strategy_info="truncated_multistep_plan_after_minimal",
                )
                retry_state.last_repair_reason = (
                    "truncated_multistep_plan_after_minimal"
                )
                planning_result = __repair_planning_output(
                    ctx=ctx,
                    planning_timeout_seconds=planning_timeout_seconds,
                    malformed_output=output_text,
                    reason="truncated_multistep_plan_after_minimal",
                    rejection_reasons=_build_repair_rejection_reasons(
                        [TRUNCATED_PLAN_REPAIR_REJECTION_REASON],
                        truncated_diagnostics,
                    ),
                    prompt_profile=prompt_profile,
                )
                retry_state.repair_prompt_used = True
                retry_state.consecutive_failures += 1
                continue

            if (
                looks_like_truncated_multistep_plan(output_text, extracted_plan)
                or repair_shrank_multistep_plan
                or (single_step_full_lifecycle_plan and retry_state.repair_prompt_used)
            ):
                normalized_plan = None
                if (
                    retry_state.repair_prompt_used
                    and isinstance(extracted_plan, list)
                    and len(extracted_plan) == 1
                ):
                    normalized_plan = _split_repaired_single_step_full_lifecycle_plan(
                        extracted_plan
                    )
                if normalized_plan:
                    ctx.logger.warning(
                        "[ORCHESTRATION] Normalized repaired single-step full-lifecycle plan into %d deterministic steps",
                        len(normalized_plan),
                    )
                    emit_phase_event(
                        ctx.orchestration_state,
                        ctx.emit_live,
                        level="WARN",
                        phase="planning",
                        message=(
                            "[ORCHESTRATION] Normalized repaired single-step "
                            "full-lifecycle plan into deterministic steps"
                        ),
                        details={
                            "reason": "single_step_full_lifecycle_plan_normalized",
                            "step_count": len(normalized_plan),
                        },
                    )
                    extracted_plan = normalized_plan
                    output_text = json.dumps(normalized_plan)
                    repair_shrank_multistep_plan = False
                    single_step_full_lifecycle_plan = False
                else:
                    truncated_diagnostics = _truncated_multistep_collapse_diagnostics(
                        output_text=output_text,
                        extracted_plan=extracted_plan,
                        repair_stage="after_first_repair",
                    )
                    if repair_shrank_multistep_plan:
                        truncated_diagnostics[
                            "truncated_multistep_original_step_count"
                        ] = retry_state.last_multistep_plan_step_count
                        truncated_diagnostics["truncated_multistep_subcodes"] = list(
                            dict.fromkeys(
                                list(
                                    truncated_diagnostics.get(
                                        "truncated_multistep_subcodes"
                                    )
                                    or []
                                )
                                + [
                                    "repair_shrank_previously_valid_multistep_plan",
                                    (
                                        "original_steps_detected_"
                                        f"{retry_state.last_multistep_plan_step_count}"
                                    ),
                                ]
                            )
                        )
                    failure_type = (
                        "single_step_full_lifecycle_plan_after_repair"
                        if (
                            single_step_full_lifecycle_plan
                            and retry_state.repair_prompt_used
                        )
                        else "truncated_multistep_plan_after_retry"
                    )
                    ctx.orchestration_state.status = OrchestrationStatus.ABORTED
                    ctx.orchestration_state.abort_reason = (
                        "Planning output collapsed into a single-step plan"
                    )
                    emit_phase_event(
                        ctx.orchestration_state,
                        ctx.emit_live,
                        level="ERROR",
                        phase="planning",
                        message="[ORCHESTRATION] Planning output was still a single-step plan after repair",
                        details={"reason": failure_type},
                    )
                    _emit_planning_diagnostics_contract_violation(
                        ctx,
                        reason=failure_type,
                        contract_violations=[
                            "full-lifecycle planning returned a single-step plan after repair"
                        ],
                        contract_diagnostics=truncated_diagnostics,
                        output_text=output_text,
                        strategy_info=failure_type,
                    )
                    _finalize_planning_terminal_failure(
                        ctx=ctx,
                        failure_type=failure_type,
                        failure_reason=(
                            "Planning output was still a single-step plan after repair. "
                            "The run was stopped to avoid a false success."
                        ),
                    )
                    if ctx.restore_workspace_snapshot_if_needed:
                        ctx.restore_workspace_snapshot_if_needed(
                            "single-step full-lifecycle plan"
                        )
                    return {
                        "status": "failed",
                        "reason": failure_type,
                    }

            if extracted_plan is None:
                plan_shape = type(plan_data).__name__
                plan_keys = (
                    sorted(plan_data.keys()) if isinstance(plan_data, dict) else []
                )
                raise ValueError(
                    "Planning result is not a recognized list of steps "
                    f"(type={plan_shape}, keys={plan_keys}, preview={str(plan_data)[:240]})"
                )

            sanitized_plan = PlannerService.sanitize_common_plan_issues(
                extracted_plan, task_prompt=ctx.prompt
            )
            sanitized_plan = _strengthen_weak_expected_file_verifications(
                sanitized_plan
            )
            sanitized_plan, static_site_normalization = (
                normalize_existing_static_site_plan(
                    sanitized_plan,
                    project_dir=ctx.orchestration_state.project_dir,
                )
            )
            if static_site_normalization.get("changed"):
                ctx.logger.info(
                    "[ORCHESTRATION] Normalized existing static-site plan paths: %s",
                    static_site_normalization,
                )
                emit_phase_event(
                    ctx.orchestration_state,
                    ctx.emit_live,
                    level="INFO",
                    phase="planning",
                    message="[ORCHESTRATION] Normalized existing static-site plan paths",
                    details=static_site_normalization,
                )
            sanitized_plan, completion_details = complete_repaired_plan_contract(
                sanitized_plan,
                task_prompt=ctx.prompt,
                repaired=retry_state.repair_prompt_used,
            )
            if completion_details.get("changed"):
                ctx.logger.info(
                    "[ORCHESTRATION] Completed deterministic plan contract gaps: %s",
                    completion_details,
                )
                emit_phase_event(
                    ctx.orchestration_state,
                    ctx.emit_live,
                    level="INFO",
                    phase="planning",
                    message="[ORCHESTRATION] Completed deterministic plan contract gaps",
                    details=completion_details,
                )
            static_validation_plan = _static_site_validation_fallback_plan(ctx)
            if static_validation_plan:
                sanitized_plan = static_validation_plan
                output_text = json.dumps(static_validation_plan)
                emit_phase_event(
                    ctx.orchestration_state,
                    ctx.emit_live,
                    level="INFO",
                    phase="planning",
                    message="[ORCHESTRATION] Replaced static-site validation plan with deterministic read-only checks",
                    details={
                        "reason": "static_site_validation_contract",
                        "workflow_stage": ctx.workflow_stage,
                    },
                )
            try:
                ctx.orchestration_state.plan = normalize_plan_with_live_logging(
                    ctx.db,
                    ctx.session_id,
                    ctx.task_id,
                    sanitized_plan,
                    ctx.orchestration_state.project_dir,
                    ctx.logger,
                    ctx.session_instance_id,
                    "Planning output",
                )
            except workspace_violation_error_cls as exc:
                if _is_repairable_malformed_shell_quoting_violation(exc):
                    second_repair_reason = None
                    if retry_state.repair_prompt_used:
                        second_repair_reason = _get_targeted_second_repair_reason(
                            retry_state=retry_state,
                            malformed_shell_quoting_violation=True,
                        )
                        if not second_repair_reason or second_repair_reason.cap_used:
                            raise
                    contract_violations = [
                        "Plan contains malformed shell quoting in runnable commands"
                    ]
                    repair_reason = (
                        second_repair_reason.retry_reason
                        if second_repair_reason
                        else "malformed_shell_quoting"
                    )
                    rejection_reasons = (
                        [second_repair_reason.rejection_text]
                        if second_repair_reason
                        else [
                            "Malformed shell quoting: emit one valid shell command "
                            "string; avoid unmatched quotes, mixed quote escaping, "
                            "and python -c snippets with nested quotes"
                        ]
                    )
                    retry_state.last_repair_reason = repair_reason
                    _emit_planning_diagnostics_contract_violation(
                        ctx,
                        reason=repair_reason,
                        contract_violations=contract_violations,
                        semantic_violation_codes=["malformed_shell_quoting"],
                        output_text=output_text,
                        strategy_info="workspace_guard_malformed_shell_quoting",
                    )
                    planning_result = __repair_planning_output(
                        ctx=ctx,
                        planning_timeout_seconds=planning_timeout_seconds,
                        malformed_output=output_text,
                        reason=f"{repair_reason}: " + str(exc)[:300],
                        rejection_reasons=rejection_reasons,
                        prompt_profile=prompt_profile,
                    )
                    retry_state.repair_prompt_used = True
                    if second_repair_reason:
                        setattr(
                            retry_state,
                            second_repair_reason.cap_attribute,
                            True,
                        )
                    retry_state.consecutive_failures += 1
                    continue
                raise
            immediate_repair_issues = PlannerService.find_immediate_repair_step_issues(
                ctx.orchestration_state.plan,
                project_dir=ctx.orchestration_state.project_dir,
            )
            blocking_issue_keys = (
                "non_runnable_steps",
                "background_process_steps",
                "placeholder_only_steps",
                "weak_verification_steps",
                "stale_replace_ops_steps",
                "test_assertion_loss_ops_steps",
                "test_deletion_ops_steps",
            )
            blocking_repair_issues = {
                key: value
                for key, value in immediate_repair_issues.items()
                if key in blocking_issue_keys and value
            }
            if blocking_repair_issues and not retry_state.repair_prompt_used:
                contract_violations = (
                    PlannerService.describe_planning_contract_violations(
                        output_text=output_text,
                        parse_success=True,
                        strategy_info="plan_contains_immediate_repair_issues",
                        plan_data=plan_data,
                        extracted_plan=ctx.orchestration_state.plan,
                        immediate_repair_issues=blocking_repair_issues,
                    )
                )
                issue_fragments = []
                if blocking_repair_issues.get("non_runnable_steps"):
                    issue_fragments.append(
                        "non-runnable pseudo-commands in steps "
                        f"{blocking_repair_issues['non_runnable_steps'][:5]}"
                    )
                if blocking_repair_issues.get("background_process_steps"):
                    issue_fragments.append(
                        "background processes in steps "
                        f"{blocking_repair_issues['background_process_steps'][:5]}"
                    )
                if blocking_repair_issues.get("placeholder_only_steps"):
                    issue_fragments.append(
                        "placeholder-only implementation steps in steps "
                        f"{blocking_repair_issues['placeholder_only_steps'][:5]}"
                    )
                if blocking_repair_issues.get("weak_verification_steps"):
                    issue_fragments.append(
                        "weak verification commands in steps "
                        f"{blocking_repair_issues['weak_verification_steps'][:5]}"
                    )
                if blocking_repair_issues.get("stale_replace_ops_steps"):
                    issue_fragments.append(
                        "replace_in_file old text not found in workspace in steps "
                        f"{blocking_repair_issues['stale_replace_ops_steps'][:5]}"
                    )
                    issue_fragments.extend(
                        PlannerService.stale_replace_repair_hints(
                            ctx.orchestration_state.plan,
                            ctx.orchestration_state.project_dir,
                        )
                    )
                if blocking_repair_issues.get("test_assertion_loss_ops_steps"):
                    issue_fragments.append(
                        "test file rewrite would remove existing assertions in steps "
                        f"{blocking_repair_issues['test_assertion_loss_ops_steps'][:5]}; "
                        "preserve existing tests and assertion intent"
                    )
                if blocking_repair_issues.get("test_deletion_ops_steps"):
                    issue_fragments.append(
                        "test file deletion in steps "
                        f"{blocking_repair_issues['test_deletion_ops_steps'][:5]}; "
                        "do not delete existing tests during fallback repair"
                    )
                retry_state.last_repair_reason = "plan_contains_immediate_repair_issues"
                semantic_violation_codes = _semantic_codes_for_immediate_repair_issues(
                    blocking_repair_issues
                )
                validation_knowledge_ctx = _retrieve_validation_repair_knowledge(
                    ctx,
                    query="Plan immediate repair issue: "
                    + "; ".join(issue_fragments[:4]),
                    failure_signature=(
                        "; ".join(semantic_violation_codes)
                        if semantic_violation_codes
                        else "plan_contains_immediate_repair_issues"
                    ),
                )
                emit_phase_event(
                    ctx.orchestration_state,
                    ctx.emit_live,
                    level="WARN",
                    phase="planning",
                    message="[ORCHESTRATION] Planning output violated the runnable-step contract; starting repair pass",
                    details={
                        "reason": "plan_contains_immediate_repair_issues",
                        "contract_violations": contract_violations[:8],
                        "semantic_violation_codes": semantic_violation_codes,
                    },
                )
                ctx.logger.warning(
                    "[ORCHESTRATION] Planning contract violation before repair: %s",
                    "; ".join(contract_violations),
                )
                _emit_planning_diagnostics_contract_violation(
                    ctx,
                    reason="plan_contains_immediate_repair_issues",
                    contract_violations=contract_violations,
                    semantic_violation_codes=semantic_violation_codes,
                    output_text=output_text,
                    strategy_info="plan_contains_immediate_repair_issues",
                )
                planning_result = __repair_planning_output(
                    ctx=ctx,
                    planning_timeout_seconds=planning_timeout_seconds,
                    malformed_output=output_text,
                    reason="plan_contains_immediate_repair_issues: "
                    + "; ".join(issue_fragments),
                    rejection_reasons=issue_fragments,
                    prompt_profile=prompt_profile,
                    knowledge_context=(
                        validation_knowledge_ctx
                        if (
                            validation_knowledge_ctx
                            and validation_knowledge_ctx.retrieved_items
                        )
                        else None
                    ),
                )
                retry_state.repair_prompt_used = True
                retry_state.consecutive_failures += 1
                continue
            if blocking_repair_issues:
                second_repair_reason = _get_targeted_second_repair_reason(
                    retry_state=retry_state,
                    blocking_repair_issues=blocking_repair_issues,
                )
                if second_repair_reason and not second_repair_reason.cap_used:
                    issue_fragments = [second_repair_reason.rejection_text]
                    if second_repair_reason.issue_key == "stale_replace_ops_steps":
                        issue_fragments.extend(
                            PlannerService.stale_replace_fallback_hints(
                                ctx.orchestration_state.plan,
                                ctx.orchestration_state.project_dir,
                            )
                        )
                    contract_violations = (
                        PlannerService.describe_planning_contract_violations(
                            output_text=output_text,
                            parse_success=True,
                            strategy_info=second_repair_reason.retry_reason,
                            plan_data=plan_data,
                            extracted_plan=ctx.orchestration_state.plan,
                            immediate_repair_issues=blocking_repair_issues,
                        )
                    )
                    emit_phase_event(
                        ctx.orchestration_state,
                        ctx.emit_live,
                        level="WARN",
                        phase="planning",
                        message=(
                            "[ORCHESTRATION] Planning repair still had a targeted "
                            "blocking issue; starting one targeted second repair pass"
                        ),
                        details={
                            "reason": second_repair_reason.event_reason,
                            second_repair_reason.issue_key: (
                                second_repair_reason.step_numbers
                            ),
                            "contract_violations": contract_violations[:8],
                            "repair_attempts": retry_state.consecutive_failures + 1,
                            "fallback_strategy": (
                                "structured_rewrite_or_preserved_write_file"
                                if second_repair_reason.issue_key
                                == "stale_replace_ops_steps"
                                else None
                            ),
                        },
                    )
                    ctx.logger.warning(
                        "[ORCHESTRATION] Planning repair still had %s in steps %s; "
                        "starting one targeted second repair pass",
                        second_repair_reason.issue_label,
                        second_repair_reason.step_numbers,
                    )
                    _emit_planning_diagnostics_contract_violation(
                        ctx,
                        reason=second_repair_reason.event_reason,
                        contract_violations=contract_violations,
                        semantic_violation_codes=[
                            second_repair_reason.semantic_violation_code
                        ],
                        output_text=output_text,
                        strategy_info=second_repair_reason.event_reason,
                    )
                    validation_knowledge_ctx = _retrieve_validation_repair_knowledge(
                        ctx,
                        query="Plan immediate repair still failed after repair: "
                        + "; ".join(issue_fragments[:4]),
                        failure_signature=second_repair_reason.semantic_violation_code,
                    )
                    retry_state.last_repair_reason = second_repair_reason.event_reason
                    planning_result = __repair_planning_output(
                        ctx=ctx,
                        planning_timeout_seconds=planning_timeout_seconds,
                        malformed_output=output_text,
                        reason=f"{second_repair_reason.retry_reason}: "
                        + "; ".join(issue_fragments),
                        rejection_reasons=issue_fragments,
                        prompt_profile=prompt_profile,
                        knowledge_context=(
                            validation_knowledge_ctx
                            if (
                                validation_knowledge_ctx
                                and validation_knowledge_ctx.retrieved_items
                            )
                            else None
                        ),
                    )
                    setattr(
                        retry_state,
                        second_repair_reason.cap_attribute,
                        True,
                    )
                    retry_state.consecutive_failures += 1
                    continue

                ctx.orchestration_state.status = OrchestrationStatus.ABORTED
                ctx.orchestration_state.abort_reason = "Planning repair still produced non-runnable or long-running commands"
                failure_reason = (
                    "Planning repair still produced invalid commands: "
                    + "; ".join(
                        f"{key}={value[:5]}"
                        for key, value in blocking_repair_issues.items()
                    )
                )
                _finalize_planning_terminal_failure(
                    ctx=ctx,
                    failure_type="planning_invalid_commands_after_repair",
                    failure_reason=failure_reason,
                )
                return {
                    "status": "failed",
                    "reason": "planning_invalid_commands_after_repair",
                }
            plan_verdict = ValidatorService.validate_plan(
                ctx.orchestration_state.plan,
                output_text=output_text,
                task_prompt=ctx.prompt,
                execution_profile=ctx.execution_profile,
                project_dir=ctx.orchestration_state.project_dir,
                title=ctx.task.title if ctx.task else None,
                description=ctx.task.description if ctx.task else None,
                validation_severity=ctx.validation_severity,
                workflow_profile=ctx.workflow_profile,
                workflow_stage=ctx.workflow_stage,
            )
            if not plan_verdict.accepted and (plan_verdict.details or {}).get(
                "unmaterialized_expected_files"
            ):
                pruned_plan, prune_details = _prune_unmaterialized_expected_files(
                    ctx.orchestration_state.plan,
                    (plan_verdict.details or {}).get("unmaterialized_expected_files")
                    or [],
                )
                if prune_details.get("changed"):
                    ctx.orchestration_state.plan = pruned_plan
                    output_text = json.dumps(pruned_plan)
                    ctx.logger.info(
                        "[ORCHESTRATION] Pruned unmaterialized expected_files from plan: %s",
                        prune_details,
                    )
                    emit_phase_event(
                        ctx.orchestration_state,
                        ctx.emit_live,
                        level="INFO",
                        phase="planning",
                        message="[ORCHESTRATION] Pruned unmaterialized expected_files from plan",
                        details=prune_details,
                    )
                    plan_verdict = ValidatorService.validate_plan(
                        ctx.orchestration_state.plan,
                        output_text=output_text,
                        task_prompt=ctx.prompt,
                        execution_profile=ctx.execution_profile,
                        project_dir=ctx.orchestration_state.project_dir,
                        title=ctx.task.title if ctx.task else None,
                        description=ctx.task.description if ctx.task else None,
                        validation_severity=ctx.validation_severity,
                        workflow_profile=ctx.workflow_profile,
                        workflow_stage=ctx.workflow_stage,
                    )
            if not plan_verdict.accepted and (
                (plan_verdict.details or {}).get("read_only_stage_mutation_steps")
                or (plan_verdict.details or {}).get("missing_workspace_expected_files")
            ):
                fallback_plan = _read_only_stage_fallback_plan(ctx)
                if fallback_plan:
                    verdict_details = plan_verdict.details or {}
                    emit_phase_event(
                        ctx.orchestration_state,
                        ctx.emit_live,
                        level="WARN",
                        phase="planning",
                        message=(
                            "[ORCHESTRATION] Replaced mutating read-only stage plan "
                            "with deterministic inspection plan"
                        ),
                        details={
                            "reason": "read_only_stage_plan_normalized",
                            "workflow_stage": ctx.workflow_stage,
                            "mutating_steps": verdict_details.get(
                                "read_only_stage_mutation_steps"
                            ),
                            "missing_workspace_expected_files": verdict_details.get(
                                "missing_workspace_expected_files"
                            ),
                        },
                    )
                    ctx.orchestration_state.plan = fallback_plan
                    output_text = json.dumps(fallback_plan)
                    plan_verdict = ValidatorService.validate_plan(
                        ctx.orchestration_state.plan,
                        output_text=output_text,
                        task_prompt=ctx.prompt,
                        execution_profile=ctx.execution_profile,
                        project_dir=ctx.orchestration_state.project_dir,
                        title=ctx.task.title if ctx.task else None,
                        description=ctx.task.description if ctx.task else None,
                        validation_severity=ctx.validation_severity,
                        workflow_profile=ctx.workflow_profile,
                        workflow_stage=ctx.workflow_stage,
                    )
            record_validation_verdict(
                ctx.db,
                ctx.session_id,
                ctx.task_id,
                ctx.orchestration_state,
                plan_verdict.verdict,
                parent_event_id=(planning_phase_event or {}).get("event_id"),
            )
            if not plan_verdict.accepted or plan_verdict.warning:
                try:
                    maybe_emit_divergence_detected(
                        project_dir=ctx.orchestration_state.project_dir,
                        session_id=ctx.session_id,
                        task_id=ctx.task_id,
                        parent_event_id=(planning_phase_event or {}).get("event_id"),
                    )
                except Exception as exc:
                    ctx.logger.debug(
                        "[ORCHESTRATION] Failed to emit planning divergence signal: %s",
                        exc,
                    )
            ctx.db.commit()
            schema_validation = (plan_verdict.details or {}).get("plan_schema") or {}
            if not schema_validation.get("valid", True):
                ctx.logger.warning(
                    "[ORCHESTRATION] Planning schema mismatch session_id=%s task_id=%s "
                    "errors=%s details=%s",
                    ctx.session_id,
                    ctx.task_id,
                    schema_validation.get("errors", []),
                    schema_validation.get("details", {}),
                )
            try:
                phase_finished_event = append_orchestration_event(
                    project_dir=ctx.orchestration_state.project_dir,
                    session_id=ctx.session_id,
                    task_id=ctx.task_id,
                    event_type=EventType.PHASE_FINISHED,
                    parent_event_id=(planning_phase_event or {}).get("event_id"),
                    details={
                        "phase": "planning",
                        "status": plan_verdict.status,
                        "step_count": len(ctx.orchestration_state.plan),
                    },
                )
                write_orchestration_state_snapshot(
                    project_dir=ctx.orchestration_state.project_dir,
                    session_id=ctx.session_id,
                    task_id=ctx.task_id,
                    orchestration_state=ctx.orchestration_state,
                    trigger="phase_finished",
                    related_event_id=phase_finished_event.get("event_id"),
                )
            except Exception as exc:
                ctx.logger.debug(
                    "[ORCHESTRATION] Failed to persist planning phase finish event/snapshot: %s",
                    exc,
                )

            if not plan_verdict.accepted and not retry_state.repair_prompt_used:
                contract_diagnostics = _plan_contract_diagnostics(plan_verdict.details)
                semantic_violation_codes = list(
                    (plan_verdict.details or {}).get("semantic_violation_codes") or []
                )
                _emit_planning_diagnostics_contract_violation(
                    ctx,
                    reason="plan_validation_failed",
                    contract_violations=plan_verdict.reasons,
                    semantic_violation_codes=semantic_violation_codes,
                    contract_diagnostics=contract_diagnostics,
                    output_text=output_text,
                    strategy_info="plan_validation_failed",
                )
                ctx.logger.warning(
                    "[ORCHESTRATION] Plan validation failed, calling repair (failure_count=%d)",
                    retry_state.consecutive_failures,
                )
                validation_knowledge_ctx = _retrieve_knowledge(
                    ctx,
                    trigger_phase="validation",
                    knowledge_types=[
                        "failure_memory",
                        "format_guide",
                        "debug_case",
                    ],
                    query="Plan validation failed: "
                    + "; ".join(plan_verdict.reasons[:3]),
                    failure_signature=(
                        plan_verdict.reasons[0] if plan_verdict.reasons else None
                    ),
                )
                if validation_knowledge_ctx:
                    _log_knowledge_usage(
                        ctx, validation_knowledge_ctx, used_in_prompt=True
                    )
                retry_state.last_repair_reason = "plan_validation_failed"
                planning_result = __repair_planning_output(
                    ctx=ctx,
                    planning_timeout_seconds=planning_timeout_seconds,
                    malformed_output=output_text,
                    reason="plan_validation_failed: "
                    + "; ".join(plan_verdict.reasons[:3]),
                    rejection_reasons=_build_repair_rejection_reasons(
                        plan_verdict.reasons,
                        plan_verdict.details,
                    ),
                    prompt_profile=prompt_profile,
                    knowledge_context=(
                        validation_knowledge_ctx
                        if (
                            validation_knowledge_ctx
                            and validation_knowledge_ctx.retrieved_items
                        )
                        else None
                    ),
                )
                retry_state.repair_prompt_used = True
                retry_state.consecutive_failures += 1
                continue

            if not plan_verdict.accepted:
                second_repair_reason = _get_targeted_second_repair_reason(
                    retry_state=retry_state,
                    plan_verdict=plan_verdict,
                )
                if second_repair_reason and not second_repair_reason.cap_used:
                    issue_fragments = [second_repair_reason.rejection_text]
                    contract_diagnostics = _plan_contract_diagnostics(
                        plan_verdict.details
                    )
                    _emit_planning_diagnostics_contract_violation(
                        ctx,
                        reason=second_repair_reason.event_reason,
                        contract_violations=plan_verdict.reasons,
                        semantic_violation_codes=[
                            second_repair_reason.semantic_violation_code
                        ],
                        contract_diagnostics=contract_diagnostics,
                        output_text=output_text,
                        strategy_info=second_repair_reason.event_reason,
                    )
                    emit_phase_event(
                        ctx.orchestration_state,
                        ctx.emit_live,
                        level="WARN",
                        phase="planning",
                        message=(
                            "[ORCHESTRATION] Planning repair still missed "
                            "verification; starting one targeted second repair pass"
                        ),
                        details={
                            "reason": second_repair_reason.event_reason,
                            second_repair_reason.issue_key: (
                                second_repair_reason.step_numbers
                            ),
                            "validation_reasons": list(plan_verdict.reasons or [])[:5],
                            "repair_attempts": retry_state.consecutive_failures + 1,
                        },
                    )
                    ctx.logger.warning(
                        "[ORCHESTRATION] Planning repair still missed verification "
                        "in steps %s; starting one targeted second repair pass",
                        second_repair_reason.step_numbers,
                    )
                    validation_knowledge_ctx = _retrieve_knowledge(
                        ctx,
                        trigger_phase="validation",
                        knowledge_types=[
                            "failure_memory",
                            "format_guide",
                            "debug_case",
                        ],
                        query="Plan validation failed after repair: "
                        + "; ".join(plan_verdict.reasons[:3]),
                        failure_signature=(
                            plan_verdict.reasons[0] if plan_verdict.reasons else None
                        ),
                    )
                    if validation_knowledge_ctx:
                        _log_knowledge_usage(
                            ctx, validation_knowledge_ctx, used_in_prompt=True
                        )
                    retry_state.last_repair_reason = second_repair_reason.event_reason
                    planning_result = __repair_planning_output(
                        ctx=ctx,
                        planning_timeout_seconds=planning_timeout_seconds,
                        malformed_output=output_text,
                        reason=f"{second_repair_reason.retry_reason}: "
                        + "; ".join(issue_fragments),
                        rejection_reasons=issue_fragments,
                        prompt_profile=prompt_profile,
                        knowledge_context=(
                            validation_knowledge_ctx
                            if (
                                validation_knowledge_ctx
                                and validation_knowledge_ctx.retrieved_items
                            )
                            else None
                        ),
                    )
                    setattr(
                        retry_state,
                        second_repair_reason.cap_attribute,
                        True,
                    )
                    retry_state.consecutive_failures += 1
                    continue

                # Repair already attempted and validation still fails — abort
                # instead of looping through more repair calls (prevents the
                # plan→error→repair→retry chain from burning minutes of budget).
                ctx.orchestration_state.status = OrchestrationStatus.ABORTED
                ctx.orchestration_state.abort_reason = (
                    "Planning validation failed after repair: "
                    + "; ".join(plan_verdict.reasons[:3])
                )
                ctx.logger.warning(
                    "[ORCHESTRATION] Plan validation still failing after repair; aborting to prevent retry loop (failure_count=%d)",
                    retry_state.consecutive_failures,
                )
                emit_phase_event(
                    ctx.orchestration_state,
                    ctx.emit_live,
                    level="ERROR",
                    phase="planning",
                    message="[ORCHESTRATION] Plan validation failed after repair",
                    details=_terminal_validation_failure_details(plan_verdict),
                )
                failure_reason = "Plan validation failed after repair: " + "; ".join(
                    plan_verdict.reasons[:4]
                )
                _finalize_planning_terminal_failure(
                    ctx=ctx,
                    failure_type="planning_validation_failed_after_repair",
                    failure_reason=failure_reason,
                )
                if ctx.restore_workspace_snapshot_if_needed:
                    ctx.restore_workspace_snapshot_if_needed(
                        "planning validation failure"
                    )
                return {
                    "status": "failed",
                    "reason": "planning_validation_failed_after_repair",
                }

            reasoning_artifact = _build_reasoning_artifact(
                ctx=ctx,
                workspace_review=workspace_review,
            )
            reasoning_verdict = ValidatorService.validate_reasoning_artifact(
                reasoning_artifact,
                plan=ctx.orchestration_state.plan,
                validation_severity=ctx.validation_severity,
            )
            record_validation_verdict(
                ctx.db,
                ctx.session_id,
                ctx.task_id,
                ctx.orchestration_state,
                reasoning_verdict,
                parent_event_id=(planning_phase_event or {}).get("event_id"),
            )
            if not reasoning_verdict.accepted:
                ctx.logger.warning(
                    "[ORCHESTRATION] Reasoning artifact validation failed after plan acceptance"
                )
                ctx.orchestration_state.status = OrchestrationStatus.ABORTED
                ctx.orchestration_state.abort_reason = (
                    "Structured reasoning artifact failed validation before execution"
                )
                emit_phase_event(
                    ctx.orchestration_state,
                    ctx.emit_live,
                    level="ERROR",
                    phase="planning",
                    message="[ORCHESTRATION] Structured reasoning artifact failed validation before execution",
                    details={
                        "reason": "reasoning_artifact_validation_failed",
                        "validation_status": reasoning_verdict.status,
                    },
                )
                failure_reason = (
                    "Structured reasoning artifact failed validation: "
                    + "; ".join(reasoning_verdict.reasons[:4])
                )
                _finalize_planning_terminal_failure(
                    ctx=ctx,
                    failure_type="reasoning_artifact_validation_failed",
                    failure_reason=failure_reason,
                )
                if ctx.restore_workspace_snapshot_if_needed:
                    ctx.restore_workspace_snapshot_if_needed(
                        "reasoning artifact validation failed"
                    )
                return {
                    "status": "failed",
                    "reason": "reasoning_artifact_validation_failed",
                }

            ctx.orchestration_state.reasoning_artifact = reasoning_artifact
            try:
                append_orchestration_event(
                    project_dir=ctx.orchestration_state.project_dir,
                    session_id=ctx.session_id,
                    task_id=ctx.task_id,
                    event_type=EventType.REASONING_ARTIFACT_GENERATED,
                    parent_event_id=(planning_phase_event or {}).get("event_id"),
                    details={
                        "intent": reasoning_artifact.get("intent"),
                        "workspace_fact_count": len(
                            reasoning_artifact.get("workspace_facts", [])
                        ),
                        "planned_action_count": len(
                            reasoning_artifact.get("planned_actions", [])
                        ),
                        "verification_count": len(
                            reasoning_artifact.get("verification_plan", [])
                        ),
                        "validation_status": reasoning_verdict.status,
                    },
                )
            except Exception as exc:
                ctx.logger.debug(
                    "[ORCHESTRATION] Failed to persist reasoning artifact event: %s",
                    exc,
                )

            ctx.logger.info(
                "[ORCHESTRATION] Generated %s steps in plan (using %s)",
                len(ctx.orchestration_state.plan),
                strategy_info,
            )
            emit_phase_event(
                ctx.orchestration_state,
                ctx.emit_live,
                level="INFO",
                phase="planning",
                message=f"[ORCHESTRATION] Generated {len(ctx.orchestration_state.plan)} steps in plan",
                details={
                    "steps": len(ctx.orchestration_state.plan),
                    "strategy": strategy_info,
                },
            )
            ctx.task.steps = json.dumps(ctx.orchestration_state.plan)
            ctx.task.current_step = 0
            ctx.db.commit()
            return {"status": "completed"}
    except PlanningRepairBudgetExceeded as exc:
        failure_type = "planning_repair_prompt_too_large"
        ctx.logger.error(
            "[ORCHESTRATION] Planning repair was skipped because the repair prompt exceeded the safe budget: %s",
            exc,
        )
        ctx.orchestration_state.status = OrchestrationStatus.ABORTED
        ctx.orchestration_state.abort_reason = str(exc)
        emit_phase_event(
            ctx.orchestration_state,
            ctx.emit_live,
            level="ERROR",
            phase="planning",
            message=f"[ORCHESTRATION] Planning repair prompt exceeded safe budget: {exc}",
            details={"reason": failure_type},
        )
        _finalize_planning_timeout_failure(
            ctx=ctx,
            failure_type=failure_type,
            failure_reason=str(exc),
        )
        if ctx.restore_workspace_snapshot_if_needed:
            ctx.restore_workspace_snapshot_if_needed(
                "planning repair prompt budget exceeded"
            )
        return {"status": "failed", "reason": failure_type}
    except TaskOperationContractViolation as exc:
        failure_type = "op_contract_violation"
        ctx.orchestration_state.status = OrchestrationStatus.ABORTED
        ctx.orchestration_state.abort_reason = f"Operation contract violation: {exc}"
        emit_phase_event(
            ctx.orchestration_state,
            ctx.emit_live,
            level="ERROR",
            phase="planning",
            message=f"[ORCHESTRATION] Planning output blocked: {exc}",
            details={"reason": failure_type},
        )
        _finalize_planning_terminal_failure(
            ctx=ctx,
            failure_type=failure_type,
            failure_reason=str(exc),
        )
        if ctx.restore_workspace_snapshot_if_needed:
            ctx.restore_workspace_snapshot_if_needed("operation contract violation")
        return {"status": "failed", "reason": failure_type}
    except workspace_violation_error_cls as exc:
        ctx.orchestration_state.status = OrchestrationStatus.ABORTED
        ctx.orchestration_state.abort_reason = f"Workspace isolation violation: {exc}"
        emit_phase_event(
            ctx.orchestration_state,
            ctx.emit_live,
            level="ERROR",
            phase="planning",
            message=f"[ORCHESTRATION] Planning output blocked: {exc}",
            details={"reason": "workspace_isolation_violation"},
        )
        _finalize_planning_terminal_failure(
            ctx=ctx,
            failure_type="workspace_isolation_violation",
            failure_reason=str(exc),
        )
        if ctx.restore_workspace_snapshot_if_needed:
            ctx.restore_workspace_snapshot_if_needed("workspace isolation violation")
        return {"status": "failed", "reason": "workspace_isolation_violation"}
    except PlanningRepairOutputContractViolation as exc:
        failure_type = "repair_output_contract_violation"
        ctx.logger.error(
            "[ORCHESTRATION] Planning repair output contract violation: %s",
            exc,
        )
        ctx.orchestration_state.status = OrchestrationStatus.ABORTED
        ctx.orchestration_state.abort_reason = str(exc)
        emit_phase_event(
            ctx.orchestration_state,
            ctx.emit_live,
            level="ERROR",
            phase="planning",
            message=f"[ORCHESTRATION] Planning repair output contract violation: {exc}",
            details={"reason": failure_type},
        )
        try:
            phase_finished_event = append_orchestration_event(
                project_dir=ctx.orchestration_state.project_dir,
                session_id=ctx.session_id,
                task_id=ctx.task_id,
                event_type=EventType.PHASE_FINISHED,
                parent_event_id=(planning_phase_event or {}).get("event_id"),
                details={
                    "phase": "planning",
                    "status": "repair_output_contract_violation",
                },
            )
            write_orchestration_state_snapshot(
                project_dir=ctx.orchestration_state.project_dir,
                session_id=ctx.session_id,
                task_id=ctx.task_id,
                orchestration_state=ctx.orchestration_state,
                trigger="phase_finished",
                related_event_id=phase_finished_event.get("event_id"),
            )
        except Exception as snap_exc:
            ctx.logger.debug(
                "[ORCHESTRATION] Failed to persist repair output contract violation phase-finish snapshot: %s",
                snap_exc,
            )
        _finalize_planning_terminal_failure(
            ctx=ctx,
            failure_type=failure_type,
            failure_reason=str(exc),
        )
        if ctx.restore_workspace_snapshot_if_needed:
            ctx.restore_workspace_snapshot_if_needed("repair output contract violation")
        return {"status": "failed", "reason": failure_type}
    except TimeoutError as exc:
        timeout_exc = exc
        failure_type = _classify_planning_timeout_failure(exc, retry_state)
        is_repair_timeout = (
            "repair_timeout" in failure_type
            or failure_type == "planning_repair_no_output_timeout"
        )
        if is_repair_timeout:
            ctx.logger.error(
                "[ORCHESTRATION] Planning repair timed out before a valid plan was produced: %s",
                timeout_exc,
            )
        else:
            ctx.logger.error(
                "[ORCHESTRATION] Planning timed out or exceeded context before a valid plan was produced: %s",
                timeout_exc,
            )
        ctx.orchestration_state.status = OrchestrationStatus.ABORTED
        ctx.orchestration_state.abort_reason = (
            f"Planning repair timed out: {timeout_exc}"
            if is_repair_timeout
            else f"Planning timed out or exceeded context: {timeout_exc}"
        )
        failure_message = (
            f"[ORCHESTRATION] Planning repair timed out: {timeout_exc}"
            if is_repair_timeout
            else (
                "[ORCHESTRATION] Planning timed out or exceeded context: "
                f"{timeout_exc}"
            )
        )
        emit_phase_event(
            ctx.orchestration_state,
            ctx.emit_live,
            level="ERROR",
            phase="planning",
            message=failure_message,
            details={"reason": failure_type},
        )
        try:
            phase_finished_event = append_orchestration_event(
                project_dir=ctx.orchestration_state.project_dir,
                session_id=ctx.session_id,
                task_id=ctx.task_id,
                event_type=EventType.PHASE_FINISHED,
                parent_event_id=(planning_phase_event or {}).get("event_id"),
                details={
                    "phase": "planning",
                    "status": (
                        "repair_timeout"
                        if is_repair_timeout
                        else "timeout_or_context_overflow"
                    ),
                },
            )
            write_orchestration_state_snapshot(
                project_dir=ctx.orchestration_state.project_dir,
                session_id=ctx.session_id,
                task_id=ctx.task_id,
                orchestration_state=ctx.orchestration_state,
                trigger="phase_finished",
                related_event_id=phase_finished_event.get("event_id"),
            )
        except Exception as exc:
            ctx.logger.debug(
                "[ORCHESTRATION] Failed to persist planning timeout phase-finish snapshot: %s",
                exc,
            )
        _finalize_planning_timeout_failure(
            ctx=ctx,
            failure_type=failure_type,
            failure_reason=str(timeout_exc),
        )
        if ctx.restore_workspace_snapshot_if_needed:
            ctx.restore_workspace_snapshot_if_needed(
                "planning repair timeout"
                if is_repair_timeout
                else "planning timeout or context overflow"
            )
        return {"status": "failed", "reason": failure_type}
    except SoftTimeLimitExceeded as exc:
        failure_type = _classify_planning_timeout_failure(exc, retry_state)
        ctx.logger.error(
            "[ORCHESTRATION] Planning was interrupted by the Celery soft time limit: %s",
            exc,
        )
        ctx.orchestration_state.status = OrchestrationStatus.ABORTED
        ctx.orchestration_state.abort_reason = "Planning exceeded the worker soft time limit before a valid plan was produced"
        emit_phase_event(
            ctx.orchestration_state,
            ctx.emit_live,
            level="ERROR",
            phase="planning",
            message=(
                "[ORCHESTRATION] Planning exceeded the worker soft time limit "
                "before a valid plan was produced"
            ),
            details={"reason": failure_type},
        )
        try:
            phase_finished_event = append_orchestration_event(
                project_dir=ctx.orchestration_state.project_dir,
                session_id=ctx.session_id,
                task_id=ctx.task_id,
                event_type=EventType.PHASE_FINISHED,
                parent_event_id=(planning_phase_event or {}).get("event_id"),
                details={
                    "phase": "planning",
                    "status": "soft_time_limit_exceeded",
                },
            )
            write_orchestration_state_snapshot(
                project_dir=ctx.orchestration_state.project_dir,
                session_id=ctx.session_id,
                task_id=ctx.task_id,
                orchestration_state=ctx.orchestration_state,
                trigger="phase_finished",
                related_event_id=phase_finished_event.get("event_id"),
            )
        except Exception as exc:
            ctx.logger.debug(
                "[ORCHESTRATION] Failed to persist planning soft-time-limit phase-finish snapshot: %s",
                exc,
            )
        _finalize_planning_timeout_failure(
            ctx=ctx,
            failure_type=failure_type,
            failure_reason=(
                "Planning exceeded the worker soft time limit before a valid plan "
                "was produced"
            ),
        )
        if ctx.restore_workspace_snapshot_if_needed:
            ctx.restore_workspace_snapshot_if_needed(
                "planning soft time limit exceeded"
            )
        return {"status": "failed", "reason": failure_type}
    except Exception as exc:
        ctx.logger.error("[ORCHESTRATION] Failed to parse planning result: %s", exc)
        ctx.orchestration_state.status = OrchestrationStatus.ABORTED
        ctx.orchestration_state.abort_reason = f"Planning parse failed: {exc}"
        emit_phase_event(
            ctx.orchestration_state,
            ctx.emit_live,
            level="ERROR",
            phase="planning",
            message=f"[ORCHESTRATION] Failed to parse planning result: {exc}",
            details={"reason": "planning_parse_error"},
        )
        try:
            phase_finished_event = append_orchestration_event(
                project_dir=ctx.orchestration_state.project_dir,
                session_id=ctx.session_id,
                task_id=ctx.task_id,
                event_type=EventType.PHASE_FINISHED,
                parent_event_id=(planning_phase_event or {}).get("event_id"),
                details={
                    "phase": "planning",
                    "status": "parse_error",
                },
            )
            write_orchestration_state_snapshot(
                project_dir=ctx.orchestration_state.project_dir,
                session_id=ctx.session_id,
                task_id=ctx.task_id,
                orchestration_state=ctx.orchestration_state,
                trigger="phase_finished",
                related_event_id=phase_finished_event.get("event_id"),
            )
        except Exception as exc:
            ctx.logger.debug(
                "[ORCHESTRATION] Failed to persist planning parse-error phase-finish snapshot: %s",
                exc,
            )
        _finalize_planning_terminal_failure(
            ctx=ctx,
            failure_type="planning_parse_error",
            failure_reason=str(exc),
        )
        if ctx.restore_workspace_snapshot_if_needed:
            ctx.restore_workspace_snapshot_if_needed("planning parse error")
        return {"status": "failed", "reason": "planning_parse_error"}


def __retry_with_minimal_prompt(
    *,
    ctx: OrchestrationRunContext,
    planning_timeout_seconds: int,
    reason: str,
    prompt_profile: str = "default",
    knowledge_context: KnowledgeContext | None = None,
) -> Dict[str, Any]:
    return PlannerService.retry_with_minimal_prompt(
        runtime_service=ctx.runtime_service,
        task_description=ctx.prompt,
        project_dir=ctx.orchestration_state.project_dir,
        timeout_seconds=planning_timeout_seconds,
        logger=ctx.logger,
        emit_live=ctx.emit_live,
        reason=reason,
        prompt_profile=prompt_profile,
        workflow_profile=ctx.workflow_profile,
        workflow_phases=getattr(ctx, "workflow_phases", []),
        workspace_has_existing_files=getattr(
            ctx, "workspace_has_existing_files", False
        ),
        knowledge_context=(
            knowledge_context
            if knowledge_context and knowledge_context.retrieved_items
            else None
        ),
    )


def __repair_planning_output(
    *,
    ctx: OrchestrationRunContext,
    planning_timeout_seconds: int,
    malformed_output: str,
    reason: str,
    rejection_reasons: list[str] | None = None,
    prompt_profile: str = "default",
    knowledge_context: KnowledgeContext | None = None,
) -> Dict[str, Any]:
    return PlannerService.repair_output(
        runtime_service=ctx.runtime_service,
        task_description=ctx.prompt,
        malformed_output=malformed_output,
        project_dir=ctx.orchestration_state.project_dir,
        timeout_seconds=planning_timeout_seconds,
        logger=ctx.logger,
        emit_live=ctx.emit_live,
        reason=reason,
        rejection_reasons=rejection_reasons,
        prompt_profile=prompt_profile,
        workflow_profile=ctx.workflow_profile,
        workflow_phases=getattr(ctx, "workflow_phases", []),
        workspace_has_existing_files=getattr(
            ctx, "workspace_has_existing_files", False
        ),
        knowledge_context=knowledge_context,
        session_id=ctx.session_id,
        task_id=ctx.task_id,
    )


def __coerce_output_text(
    *,
    ctx: OrchestrationRunContext,
    planning_result: Any,
    output_result: Any,
    extract_structured_text: Callable[[Any], str],
) -> str:
    ctx.logger.info(
        "[ORCHESTRATION] Planning output type: %s, preview: %s",
        type(output_result),
        str(output_result)[:1500],
    )
    output_text = extract_structured_text(output_result)
    if not output_text.strip() and isinstance(output_result, dict):
        output_text = json.dumps(output_result)
        ctx.logger.info(
            "[ORCHESTRATION] Structured text extraction empty; using full JSON"
        )
    if not output_text.strip():
        fallback_text = extract_structured_text(planning_result)
        if fallback_text.strip():
            output_text = fallback_text
            ctx.logger.info(
                "[ORCHESTRATION] Output field was empty; recovered planning text from full result payload"
            )
    if (
        not output_text.strip()
        and isinstance(planning_result, dict)
        and planning_result
    ):
        output_text = json.dumps(planning_result)
        ctx.logger.info(
            "[ORCHESTRATION] Using serialized full planning result payload as final fallback"
        )
    elif isinstance(output_result, str):
        ctx.logger.info("[ORCHESTRATION] Raw string response")
    else:
        ctx.logger.info(
            "[ORCHESTRATION] Structured text extracted from %s",
            type(output_result),
        )
    if isinstance(output_text, str):
        output_text = __strip_markdown_fences(output_text)
    return output_text


def __build_planning_prompt(
    *, prompt: str, orchestration_state: Any, execution_profile: str
) -> str:
    from app.services import PromptTemplates

    return PromptTemplates.build_planning_prompt(
        task_description=prompt,
        project_context=orchestration_state.project_context,
        workspace_root=str(orchestration_state.workspace_root),
        project_dir=str(orchestration_state.project_dir),
        execution_profile=execution_profile,
    )


def __strip_markdown_fences(output_text: str) -> str:
    import re

    markdown_pattern = r"^\s*```(?:json)?\s*|\s*```$"
    return re.sub(markdown_pattern, "", output_text.strip())


def _retrieve_knowledge(
    ctx: OrchestrationRunContext,
    trigger_phase: str,
    knowledge_types: list[str],
    query: str | None = None,
    failure_signature: str | None = None,
) -> KnowledgeContext | None:
    """Retrieve knowledge context; returns None on any error so failures don't break the flow."""
    try:
        from app.config import settings
        from app.services.knowledge.knowledge_service import KnowledgeService

        svc = KnowledgeService(
            qdrant_url=settings.QDRANT_URL,
            collection_name=settings.QDRANT_COLLECTION_NAME,
        )
        knowledge_ctx = svc.retrieve(
            query=query or ctx.prompt or "",
            trigger_phase=trigger_phase,
            knowledge_types=knowledge_types,
            failure_signature=failure_signature,
            db=ctx.db,
        )
        ctx.logger.info(
            "[KNOWLEDGE] Retrieval phase=%s types=%s items=%d reason=%s "
            "matched_failure_memory=%s recommended_action=%s",
            trigger_phase,
            ",".join(knowledge_types),
            len(knowledge_ctx.retrieved_items),
            knowledge_ctx.retrieval_reason,
            knowledge_ctx.matched_failure_memory,
            knowledge_ctx.recommended_action.value,
        )
        return knowledge_ctx
    except Exception as exc:
        ctx.logger.debug("[KNOWLEDGE] Retrieval skipped (%s): %s", trigger_phase, exc)
        return None


def _log_knowledge_usage(
    ctx: OrchestrationRunContext,
    knowledge_ctx: KnowledgeContext,
    *,
    used_in_prompt: bool,
) -> None:
    try:
        from app.services.knowledge import usage_log_service

        usage_log_service.log_usage(
            context=knowledge_ctx,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            used_in_prompt=used_in_prompt,
            db=ctx.db,
        )
    except Exception as exc:
        ctx.logger.debug("[KNOWLEDGE] Usage log skipped: %s", exc)
