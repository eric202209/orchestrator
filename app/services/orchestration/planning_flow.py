"""Planning-phase orchestration flow."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Dict, List

from app.models import TaskStatus
from app.services.orchestration.persistence import record_validation_verdict
from app.services.orchestration.planner import PlannerService
from app.services.orchestration.validator import ValidatorService
from app.services.prompt_templates import OrchestrationStatus


def execute_planning_phase(
    *,
    db: Any,
    session: Any,
    task: Any,
    session_id: int,
    task_id: int,
    prompt: str,
    timeout_seconds: int,
    execution_profile: str,
    orchestration_state: Any,
    openclaw_service: Any,
    workspace_review: Dict[str, Any],
    logger: logging.Logger,
    emit_live: Callable[..., None],
    error_handler: Any,
    extract_structured_text: Callable[[Any], str],
    extract_plan_steps: Callable[[Any], Any],
    looks_like_truncated_multistep_plan: Callable[[str, Any], bool],
    normalize_plan_with_live_logging: Callable[..., Any],
    restore_workspace_snapshot_if_needed: Callable[[str], None],
    workspace_violation_error_cls: type[Exception],
) -> Dict[str, Any]:
    logger.info("[ORCHESTRATION] Phase 1: PLANNING - generating step plan")
    emit_live(
        "INFO",
        "[ORCHESTRATION] Phase 1: PLANNING - generating step plan",
        metadata={"phase": "planning"},
    )

    planning_prompt = openclaw_service and __build_planning_prompt(
        prompt=prompt,
        orchestration_state=orchestration_state,
        execution_profile=execution_profile,
    )

    planning_timeout_seconds = max(180, min(timeout_seconds, 300))
    start_with_minimal_planning_prompt = (
        PlannerService.should_start_with_minimal_prompt(
            prompt,
            orchestration_state.project_context,
        )
    )
    if workspace_review.get("has_existing_files"):
        start_with_minimal_planning_prompt = True
    used_minimal_planning_prompt = start_with_minimal_planning_prompt

    if start_with_minimal_planning_prompt:
        emit_live(
            "WARN",
            "[ORCHESTRATION] Planning context is dense; starting with minimal prompt",
            metadata={
                "phase": "planning",
                "strategy": "minimal_prompt_first",
                "project_context_length": len(
                    orchestration_state.project_context or ""
                ),
            },
        )
        planning_result = PlannerService.retry_with_minimal_prompt(
            openclaw_service=openclaw_service,
            task_description=prompt,
            project_dir=orchestration_state.project_dir,
            timeout_seconds=planning_timeout_seconds,
            logger=logger,
            emit_live=emit_live,
            reason="dense_planning_context",
        )
    else:
        planning_result = asyncio.run(
            openclaw_service.execute_task(
                planning_prompt, timeout_seconds=planning_timeout_seconds
            )
        )

    initial_output_text = extract_structured_text(planning_result.get("output", ""))
    if PlannerService.should_retry_with_minimal_prompt(
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
                "reason": (planning_result.get("error") or initial_output_text)[:240],
            },
        )
        planning_result = PlannerService.retry_with_minimal_prompt(
            openclaw_service=openclaw_service,
            task_description=prompt,
            project_dir=orchestration_state.project_dir,
            timeout_seconds=planning_timeout_seconds,
            logger=logger,
            emit_live=emit_live,
            reason=(planning_result.get("error") or initial_output_text),
        )
        used_minimal_planning_prompt = True

    try:
        used_planning_repair_prompt = False
        while True:
            output_result = planning_result.get("output", {})
            logger.info(
                "[ORCHESTRATION] Planning result keys: %s",
                (
                    list(planning_result.keys())
                    if isinstance(planning_result, dict)
                    else "Not a dict"
                ),
            )
            logger.info(
                "[ORCHESTRATION] Planning output type: %s, preview: %s",
                type(output_result),
                str(output_result)[:300],
            )
            logger.info(
                "[ORCHESTRATION] Raw planning output type: %s, content preview: %s",
                type(output_result),
                str(output_result)[:200],
            )

            output_text = extract_structured_text(output_result)
            if not output_text.strip() and isinstance(output_result, dict):
                output_text = json.dumps(output_result)
                logger.info(
                    "[ORCHESTRATION] Structured text extraction empty; using full JSON"
                )
            elif isinstance(output_result, str):
                logger.info("[ORCHESTRATION] Raw string response")
            else:
                logger.info(
                    "[ORCHESTRATION] Structured text extracted from %s",
                    type(output_result),
                )

            logger.info(
                "[ORCHESTRATION] Final extracted text length: %s", len(output_text)
            )

            if isinstance(output_text, str):
                output_text = __strip_markdown_fences(output_text)
                logger.info(
                    "[ORCHESTRATION] After stripping markdown, length: %s",
                    len(output_text),
                )

            success, plan_data, strategy_info = error_handler.attempt_json_parsing(
                output_text, context="planning"
            )

            if PlannerService.should_retry_with_minimal_prompt(
                planning_result, output_text
            ):
                raise TimeoutError(
                    f"Planning timed out or exceeded context after {planning_timeout_seconds}s"
                )

            if not success and not used_minimal_planning_prompt:
                planning_result = PlannerService.retry_with_minimal_prompt(
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

            if not success and not used_planning_repair_prompt:
                planning_result = PlannerService.repair_output(
                    openclaw_service=openclaw_service,
                    task_description=prompt,
                    malformed_output=output_text,
                    project_dir=orchestration_state.project_dir,
                    timeout_seconds=planning_timeout_seconds,
                    logger=logger,
                    emit_live=emit_live,
                    reason=f"json_parse_failed_after_minimal: {strategy_info}",
                )
                used_planning_repair_prompt = True
                continue

            if not success:
                orchestration_state.status = OrchestrationStatus.ABORTED
                orchestration_state.abort_reason = (
                    f"Planning JSON parse failed: {strategy_info}"
                )
                emit_live(
                    "ERROR",
                    f"[ORCHESTRATION] Planning JSON parse failed: {strategy_info}",
                    metadata={"phase": "planning", "reason": "planning_json_error"},
                )
                task.status = TaskStatus.FAILED
                task.error_message = (
                    f"Planning JSON parse failed: {strategy_info}. "
                    f"Raw output: {output_text[:500]}"
                )
                db.commit()
                restore_workspace_snapshot_if_needed("planning JSON parse failure")
                return {"status": "failed", "reason": "planning_json_error"}

            extracted_plan = extract_plan_steps(plan_data)
            if extracted_plan is None and not used_minimal_planning_prompt:
                planning_result = PlannerService.retry_with_minimal_prompt(
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

            if extracted_plan is None and not used_planning_repair_prompt:
                planning_result = PlannerService.repair_output(
                    openclaw_service=openclaw_service,
                    task_description=prompt,
                    malformed_output=output_text,
                    project_dir=orchestration_state.project_dir,
                    timeout_seconds=planning_timeout_seconds,
                    logger=logger,
                    emit_live=emit_live,
                    reason="unexpected_plan_shape_after_minimal",
                )
                used_planning_repair_prompt = True
                continue

            if (
                looks_like_truncated_multistep_plan(output_text, extracted_plan)
                and not used_minimal_planning_prompt
            ):
                planning_result = PlannerService.retry_with_minimal_prompt(
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

            if (
                looks_like_truncated_multistep_plan(output_text, extracted_plan)
                and not used_planning_repair_prompt
            ):
                planning_result = PlannerService.repair_output(
                    openclaw_service=openclaw_service,
                    task_description=prompt,
                    malformed_output=output_text,
                    project_dir=orchestration_state.project_dir,
                    timeout_seconds=planning_timeout_seconds,
                    logger=logger,
                    emit_live=emit_live,
                    reason="truncated_multistep_plan_after_minimal",
                )
                used_planning_repair_prompt = True
                continue

            if looks_like_truncated_multistep_plan(output_text, extracted_plan):
                orchestration_state.status = OrchestrationStatus.ABORTED
                orchestration_state.abort_reason = (
                    "Planning output collapsed a multi-step plan into a single step"
                )
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
                    "step after retry. The run was stopped to avoid a false success."
                )
                db.commit()
                restore_workspace_snapshot_if_needed("truncated multi-step plan")
                return {
                    "status": "failed",
                    "reason": "truncated_multistep_plan_after_retry",
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

            orchestration_state.plan = normalize_plan_with_live_logging(
                db,
                session_id,
                task_id,
                extracted_plan,
                orchestration_state.project_dir,
                logger,
                session.instance_id,
                "Planning output",
            )
            plan_verdict = ValidatorService.validate_plan(
                orchestration_state.plan,
                output_text=output_text,
                task_prompt=prompt,
                execution_profile=execution_profile,
                project_dir=orchestration_state.project_dir,
                title=task.title if task else None,
                description=task.description if task else None,
            )
            record_validation_verdict(
                db,
                session_id,
                task_id,
                orchestration_state,
                plan_verdict,
            )
            db.commit()
            if not plan_verdict.accepted and not used_planning_repair_prompt:
                planning_result = PlannerService.repair_output(
                    openclaw_service=openclaw_service,
                    task_description=prompt,
                    malformed_output=output_text,
                    project_dir=orchestration_state.project_dir,
                    timeout_seconds=planning_timeout_seconds,
                    logger=logger,
                    emit_live=emit_live,
                    reason="plan_validation_failed: "
                    + "; ".join(plan_verdict.reasons[:3]),
                    rejection_reasons=plan_verdict.reasons,
                )
                used_planning_repair_prompt = True
                continue
            if not plan_verdict.accepted:
                orchestration_state.status = OrchestrationStatus.ABORTED
                orchestration_state.abort_reason = (
                    "Planning output failed validation: "
                    + "; ".join(plan_verdict.reasons[:3])
                )
                emit_live(
                    "ERROR",
                    "[ORCHESTRATION] Planning output failed validation",
                    metadata={
                        "phase": "planning",
                        "reason": "planning_validation_failed",
                        "validation_status": plan_verdict.status,
                        "reasons": plan_verdict.reasons[:10],
                    },
                )
                task.status = TaskStatus.FAILED
                task.error_message = "Planning failed validation: " + "; ".join(
                    plan_verdict.reasons[:5]
                )
                db.commit()
                restore_workspace_snapshot_if_needed("planning validation failure")
                return {"status": "failed", "reason": "planning_validation_failed"}

            logger.info(
                "[ORCHESTRATION] Generated %s steps in plan (using %s)",
                len(orchestration_state.plan),
                strategy_info,
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
            return {"status": "completed"}
    except workspace_violation_error_cls as exc:
        orchestration_state.status = OrchestrationStatus.ABORTED
        orchestration_state.abort_reason = f"Workspace isolation violation: {exc}"
        emit_live(
            "ERROR",
            f"[ORCHESTRATION] Planning output blocked: {exc}",
            metadata={"phase": "planning", "reason": "workspace_isolation_violation"},
        )
        task.status = TaskStatus.FAILED
        task.error_message = str(exc)
        db.commit()
        restore_workspace_snapshot_if_needed("workspace isolation violation")
        return {"status": "failed", "reason": "workspace_isolation_violation"}
    except Exception as exc:
        logger.error("[ORCHESTRATION] Failed to parse planning result: %s", exc)
        orchestration_state.status = OrchestrationStatus.ABORTED
        orchestration_state.abort_reason = f"Planning parse failed: {exc}"
        emit_live(
            "ERROR",
            f"[ORCHESTRATION] Failed to parse planning result: {exc}",
            metadata={"phase": "planning", "reason": "planning_parse_error"},
        )
        task.status = TaskStatus.FAILED
        task.error_message = str(exc)
        db.commit()
        restore_workspace_snapshot_if_needed("planning parse error")
        return {"status": "failed", "reason": "planning_parse_error"}


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
