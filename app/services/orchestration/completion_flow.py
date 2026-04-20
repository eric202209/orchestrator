"""Task completion and finalization flow."""

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from app.models import SessionTask, Task, TaskStatus
from app.services.error_handler import error_handler
from app.services.orchestration.execution_flow import (
    assess_step_execution,
    determine_step_timeout,
)
from app.services.orchestration.parsing import extract_structured_text
from app.services.orchestration.persistence import (
    record_validation_verdict,
    save_orchestration_checkpoint,
    set_session_alert,
)
from app.services.orchestration.policy import SUMMARY_TIMEOUT_SECONDS
from app.services.orchestration.runtime import write_project_state_snapshot
from app.services.orchestration.step_support import coerce_execution_step_result
from app.services.orchestration.telemetry import emit_phase_event
from app.services.orchestration.types import OrchestrationRunContext
from app.services.orchestration.validator import ValidatorService
from app.services.prompt_templates import OrchestrationStatus, StepResult


RELATIVE_PATH_TOKEN_RE = re.compile(
    r"(?<![\w./-])("
    r"(?:src|tests|fixtures|scripts|config)/[A-Za-z0-9_./-]+"
    r"|(?:package\.json|tsconfig\.json|vitest\.config\.ts|jest\.config\.js|README\.md|\.env\.example)"
    r")(?![\w./-])"
)


def _collect_workspace_inventory_paths(
    project_dir: Path,
    max_files: int = 200,
) -> list[str]:
    existing_files: list[str] = []
    if not project_dir.exists():
        return existing_files
    for path in sorted(project_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(project_dir)
        if any(
            part in {"node_modules", ".openclaw", "__pycache__"}
            for part in relative.parts
        ):
            continue
        existing_files.append(str(relative))
        if len(existing_files) >= max_files:
            break
    return existing_files


def _build_completion_repair_workspace_summary(
    *,
    project_dir: Path,
    completion_validation: Any,
    max_files: int = 80,
) -> str:
    expected_files = (
        list((completion_validation.details or {}).get("expected_core_files", []) or [])
        if completion_validation
        else []
    )
    existing_files = [
        path
        for path in _collect_workspace_inventory_paths(
            project_dir, max_files=max_files * 2
        )
        if Path(path).suffix.lower() in {".ts", ".tsx", ".js", ".jsx", ".json", ".sh"}
    ][:max_files]

    similar_map: dict[str, list[str]] = {}
    lowered_existing = [(item, item.lower()) for item in existing_files]
    for expected in expected_files[:20]:
        expected_name = Path(expected).stem.lower().replace("-", "_")
        expected_parent = str(Path(expected).parent).lower()
        matches: list[str] = []
        for existing, lowered in lowered_existing:
            existing_name = Path(existing).stem.lower().replace("-", "_")
            if expected_name == existing_name:
                matches.append(existing)
                continue
            if (
                expected_parent
                and expected_parent in lowered
                and any(
                    token and token in existing_name
                    for token in expected_name.split("_")
                )
            ):
                matches.append(existing)
        if matches:
            similar_map[expected] = matches[:5]

    lines = [
        "Current workspace inventory:",
        *[f"- {path}" for path in existing_files[:max_files]],
    ]
    if expected_files:
        lines.append("Expected core files from the current accepted plan:")
        lines.extend(f"- {path}" for path in expected_files[:20])
    if similar_map:
        lines.append(
            "Existing files that look structurally similar to missing expected files:"
        )
        for expected, matches in similar_map.items():
            lines.append(f"- {expected} -> {', '.join(matches)}")
    return "\n".join(lines)


def _extract_relative_paths_from_text(raw_text: str) -> set[str]:
    text = str(raw_text or "")
    return {match.group(1).strip() for match in RELATIVE_PATH_TOKEN_RE.finditer(text)}


def _collect_created_paths_from_commands(
    commands: list[str],
) -> tuple[set[str], set[str]]:
    created_files: set[str] = set()
    created_dirs: set[str] = set()
    for command in commands:
        text = str(command or "").strip()
        if not text:
            continue
        for match in re.finditer(r"(?:cat|tee)\s*>\s*([A-Za-z0-9_./-]+)", text):
            created_files.add(match.group(1).strip())
        if text.startswith("touch "):
            for token in text.split()[1:]:
                cleaned = token.strip()
                if cleaned and ("/" in cleaned or "." in cleaned):
                    created_files.add(cleaned)
        if text.startswith("mkdir -p "):
            for token in text.split()[2:]:
                cleaned = token.strip()
                if cleaned:
                    created_dirs.add(cleaned.rstrip("/"))
        if text.startswith("mv "):
            parts = text.split()
            if len(parts) >= 3:
                created_files.add(parts[-1].strip())
    return created_files, created_dirs


def _completion_repair_invalid_paths(
    *,
    repair_step: Dict[str, Any],
    project_dir: Path,
    completion_validation: Any,
) -> list[str]:
    inventory_files = set(_collect_workspace_inventory_paths(project_dir))
    expected_files = set(
        list((completion_validation.details or {}).get("expected_core_files", []) or [])
    )
    commands = [str(command) for command in repair_step.get("commands", []) or []]
    created_files, created_dirs = _collect_created_paths_from_commands(commands)
    referenced_paths: set[str] = set()
    for text in commands + [
        str(repair_step.get("verification") or ""),
        str(repair_step.get("rollback") or ""),
    ]:
        referenced_paths.update(_extract_relative_paths_from_text(text))

    invalid: list[str] = []
    for path in sorted(referenced_paths):
        if (
            path in inventory_files
            or path in expected_files
            or path in created_files
            or any(
                path == directory or path.startswith(f"{directory}/")
                for directory in created_dirs
            )
        ):
            continue
        invalid.append(path)
    return invalid


def _extract_reported_changed_files(output_text: str, project_dir: Path) -> list[str]:
    reported: list[str] = []
    text = str(output_text or "")
    patterns = [
        r"`([^`]+)`",
        r"[-*]\s+([A-Za-z0-9_./-]+\.(?:ts|tsx|js|jsx|json|sh))",
        r"(src/[A-Za-z0-9_./-]+\.(?:ts|tsx|js|jsx))",
        r"(tests/[A-Za-z0-9_./-]+\.(?:ts|tsx|js|jsx))",
        r"(vitest\.config\.ts|jest\.config\.js|package\.json|tsconfig\.json|\.env\.example)",
    ]
    seen: set[str] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            candidate = match.group(1).strip()
            if not candidate or candidate in seen:
                continue
            resolved = (project_dir / candidate).resolve()
            if resolved.exists() and resolved.is_file():
                seen.add(candidate)
                reported.append(candidate)
    return reported


def _build_completion_repair_prompt(
    *,
    task_prompt: str,
    completion_validation: Any,
    project_dir: Any,
    prior_results_summary: str,
    project_context: str,
    next_step_number: int,
    workspace_inventory: str,
) -> str:
    return f"""Return one minimal JSON repair step to fix completion validation issues. Output JSON object only.

Task:
{task_prompt[:2000]}

Working directory:
{project_dir}

Completion validation issues:
{json.dumps(completion_validation.reasons[:10], indent=2)}

Prior completed results:
{prior_results_summary[:2000]}

Project context:
{project_context[:2500]}

Current workspace inventory:
{workspace_inventory[:5000]}

Rules:
1. Return a single JSON object with keys: step_number, description, commands, verification, rollback, expected_files
2. Keep the fix atomic and minimal
3. Use relative shell paths only
4. Do not use `..`, `~`, or absolute paths in commands
5. Do not create documentation files unless explicitly required
6. Do not create a new top-level project folder
7. Prefer fixing misplaced files, missing core files, or weak structure over rewriting the whole project
8. commands must be a non-empty JSON array
9. expected_files must list the files this repair should materialize or normalize
10. Use the workspace inventory above as the source of truth; do not assume older file names or architectures that are not present
11. Prefer renaming, moving, or normalizing existing files over creating parallel replacements
12. Do not read or modify a guessed file path unless it appears in the workspace inventory or your commands create it first

Output example:
{{
  "step_number": {next_step_number},
  "description": "Move generated test files into the workspace root and verify they load",
  "commands": ["mkdir -p tests", "mv nested/tests/*.spec.js tests/"],
  "verification": "test -f tests/event-chain.spec.js",
  "rollback": null,
  "expected_files": ["tests/event-chain.spec.js"]
}}
"""


def _normalize_completion_repair_step(
    raw_step: Dict[str, Any], next_step_number: int
) -> Dict[str, Any]:
    commands = raw_step.get("commands", [])
    if isinstance(commands, str):
        commands = [commands]
    if not isinstance(commands, list):
        commands = []

    expected_files = raw_step.get("expected_files", [])
    if isinstance(expected_files, str):
        expected_files = [expected_files]
    if not isinstance(expected_files, list):
        expected_files = []

    return {
        "step_number": raw_step.get("step_number") or next_step_number,
        "description": str(
            raw_step.get("description") or "Apply minimal completion repair"
        ),
        "commands": [
            str(command).strip() for command in commands if str(command).strip()
        ],
        "verification": raw_step.get("verification"),
        "rollback": raw_step.get("rollback"),
        "expected_files": [
            str(path).strip() for path in expected_files if str(path).strip()
        ],
    }


def _extract_completion_repair_step(
    parsed_data: Any, next_step_number: int
) -> Optional[Dict[str, Any]]:
    if isinstance(parsed_data, dict):
        step_like_keys = {
            "step_number",
            "description",
            "commands",
            "verification",
            "rollback",
            "expected_files",
        }
        if step_like_keys.intersection(parsed_data.keys()):
            return _normalize_completion_repair_step(parsed_data, next_step_number)

        for key in (
            "step",
            "repair_step",
            "completion_repair_step",
            "payload",
            "result",
        ):
            candidate = parsed_data.get(key)
            if isinstance(candidate, dict):
                extracted = _extract_completion_repair_step(candidate, next_step_number)
                if extracted:
                    return extracted

    if isinstance(parsed_data, list):
        for item in parsed_data:
            extracted = _extract_completion_repair_step(item, next_step_number)
            if extracted:
                return extracted

    return None


def _attempt_completion_repair(
    *,
    ctx: OrchestrationRunContext,
    completion_validation: Any,
    save_orchestration_checkpoint_fn: Callable[..., None],
) -> Dict[str, Any]:
    orchestration_state = ctx.orchestration_state
    emit_live = ctx.emit_live
    logger = ctx.logger
    task = ctx.task
    db = ctx.db
    session = ctx.session

    if orchestration_state.completion_repair_attempts >= 1:
        return {"status": "skipped", "reason": "repair_attempt_limit_reached"}

    orchestration_state.completion_repair_attempts += 1
    next_step_number = len(orchestration_state.plan) + 1

    emit_live(
        "WARN",
        "[ORCHESTRATION] Completion validation is repairable; generating one minimal repair step",
        metadata={
            "phase": "completion_repair",
            "attempt": orchestration_state.completion_repair_attempts,
            "reasons": completion_validation.reasons[:10],
        },
    )

    repair_prompt = _build_completion_repair_prompt(
        task_prompt=ctx.prompt,
        completion_validation=completion_validation,
        project_dir=orchestration_state.project_dir,
        prior_results_summary=orchestration_state.prior_results_summary(),
        project_context=orchestration_state.project_context,
        next_step_number=next_step_number,
        workspace_inventory=_build_completion_repair_workspace_summary(
            project_dir=Path(orchestration_state.project_dir),
            completion_validation=completion_validation,
        ),
    )
    repair_plan_result = asyncio.run(
        ctx.openclaw_service.execute_task(repair_prompt, timeout_seconds=120)
    )
    repair_output = extract_structured_text(repair_plan_result.get("output", "{}"))
    success, repair_data, strategy_info = error_handler.attempt_json_parsing(
        repair_output, context="completion_repair"
    )
    if not success:
        fallback_output = extract_structured_text(repair_plan_result)
        if fallback_output and fallback_output != repair_output:
            success, repair_data, strategy_info = error_handler.attempt_json_parsing(
                fallback_output, context="completion_repair"
            )

    if not success:
        logger.warning(
            "[ORCHESTRATION] Completion repair step generation failed to parse: %s",
            strategy_info,
        )
        return {
            "status": "failed",
            "reason": f"repair_step_parse_failed:{strategy_info}",
        }

    repair_step = _extract_completion_repair_step(repair_data, next_step_number)
    if repair_step is None:
        logger.warning(
            "[ORCHESTRATION] Completion repair parse succeeded but no usable step object was found"
        )
        return {
            "status": "failed",
            "reason": "repair_step_missing_step_object",
        }

    if not repair_step.get("commands"):
        return {"status": "failed", "reason": "repair_step_missing_commands"}

    invalid_paths = _completion_repair_invalid_paths(
        repair_step=repair_step,
        project_dir=Path(orchestration_state.project_dir),
        completion_validation=completion_validation,
    )
    if invalid_paths:
        logger.warning(
            "[ORCHESTRATION] Completion repair step referenced inventory-missing paths: %s",
            invalid_paths[:10],
        )
        emit_live(
            "WARN",
            "[ORCHESTRATION] Completion repair step referenced paths that are not present in the current workspace inventory; requesting one guarded retry",
            metadata={
                "phase": "completion_repair",
                "invalid_paths": invalid_paths[:10],
            },
        )
        guarded_retry_prompt = (
            repair_prompt
            + "\n\nThe previous repair step was invalid because it referenced these paths that are not present in the workspace inventory or not created by the repair step:\n"
            + json.dumps(invalid_paths[:20], indent=2)
            + "\nReturn a replacement repair step that uses only inventory-confirmed paths or creates the referenced files first."
        )
        guarded_retry_result = asyncio.run(
            ctx.openclaw_service.execute_task(guarded_retry_prompt, timeout_seconds=120)
        )
        guarded_retry_output = extract_structured_text(
            guarded_retry_result.get("output", "{}")
        )
        retry_success, retry_data, retry_strategy_info = (
            error_handler.attempt_json_parsing(
                guarded_retry_output, context="completion_repair"
            )
        )
        if not retry_success:
            fallback_output = extract_structured_text(guarded_retry_result)
            if fallback_output and fallback_output != guarded_retry_output:
                retry_success, retry_data, retry_strategy_info = (
                    error_handler.attempt_json_parsing(
                        fallback_output, context="completion_repair"
                    )
                )
        if not retry_success:
            return {
                "status": "failed",
                "reason": f"repair_step_inventory_guard_parse_failed:{retry_strategy_info}",
            }
        repair_step = _extract_completion_repair_step(retry_data, next_step_number)
        if not repair_step or not repair_step.get("commands"):
            return {
                "status": "failed",
                "reason": "repair_step_inventory_guard_missing_commands",
            }
        invalid_paths = _completion_repair_invalid_paths(
            repair_step=repair_step,
            project_dir=Path(orchestration_state.project_dir),
            completion_validation=completion_validation,
        )
        if invalid_paths:
            return {
                "status": "failed",
                "reason": "repair_step_inventory_guard_rejected:"
                + ", ".join(invalid_paths[:10]),
            }
        strategy_info = retry_strategy_info

    orchestration_state.plan.append(repair_step)
    task.steps = json.dumps(orchestration_state.plan)
    task.current_step = next_step_number
    save_orchestration_checkpoint_fn(
        db, ctx.session_id, ctx.task_id, ctx.prompt, orchestration_state
    )
    db.commit()

    emit_live(
        "INFO",
        f"[ORCHESTRATION] Executing completion repair step {next_step_number}: {repair_step['description']}",
        metadata={
            "phase": "completion_repair",
            "step_index": next_step_number,
            "strategy": strategy_info,
        },
    )

    from app.services import PromptTemplates

    execution_prompt = PromptTemplates.build_execution_prompt(
        step_description=repair_step["description"],
        step_commands=repair_step["commands"],
        project_dir=str(orchestration_state.project_dir),
        verification_command=repair_step.get("verification"),
        rollback_command=repair_step.get("rollback"),
        expected_files=repair_step.get("expected_files", []),
        completed_steps_summary=orchestration_state.prior_results_summary(),
        project_context=orchestration_state.project_context,
        execution_profile=ctx.execution_profile,
    )
    step_timeout_seconds = determine_step_timeout(
        timeout_seconds=ctx.timeout_seconds,
        total_steps=len(orchestration_state.plan),
        execution_profile=ctx.execution_profile,
        step_description=repair_step["description"],
        task_prompt=ctx.prompt,
    )
    step_started_at = datetime.utcnow()
    repair_exec_result = asyncio.run(
        ctx.openclaw_service.execute_task(
            execution_prompt,
            timeout_seconds=step_timeout_seconds,
        )
    )
    repair_exec_result = coerce_execution_step_result(
        repair_exec_result,
        expected_files=repair_step.get("expected_files", []),
        extract_structured_text=extract_structured_text,
    )
    reported_changed_files = _extract_reported_changed_files(
        str(repair_exec_result.get("output", "")),
        Path(orchestration_state.project_dir),
    )
    if reported_changed_files:
        repair_exec_result["files_changed"] = reported_changed_files
        adjusted_expected_files = [
            path
            for path in reported_changed_files
            if path.startswith(("src/", "tests/"))
            or path
            in {
                "vitest.config.ts",
                "jest.config.js",
                "package.json",
                "tsconfig.json",
                ".env.example",
            }
        ]
        if adjusted_expected_files:
            repair_step["expected_files"] = adjusted_expected_files
    assessment = assess_step_execution(
        db=db,
        session_id=ctx.session_id,
        task_id=ctx.task_id,
        project_dir=orchestration_state.project_dir,
        step=repair_step,
        step_result=repair_exec_result,
        step_started_at=step_started_at,
        validation_profile=ctx.validation_profile,
        relaxed_mode=orchestration_state.relaxed_mode,
    )
    if assessment.validation_verdict:
        record_validation_verdict(
            db,
            ctx.session_id,
            ctx.task_id,
            orchestration_state,
            assessment.validation_verdict,
            step_number=next_step_number,
        )
        db.commit()

    step_record = StepResult(
        step_number=next_step_number,
        status=assessment.step_status,
        output=assessment.step_output[:1000],
        verification_output=repair_exec_result.get("verification_output", ""),
        files_changed=repair_exec_result.get(
            "files_changed", repair_step.get("expected_files", [])
        ),
        error_message=assessment.error_message,
        attempt=1,
    )

    if assessment.step_status == "success":
        orchestration_state.record_success(step_record)
        task.current_step = len(orchestration_state.plan)
        save_orchestration_checkpoint_fn(
            db, ctx.session_id, ctx.task_id, ctx.prompt, orchestration_state
        )
        db.commit()
        emit_live(
            "INFO",
            f"[ORCHESTRATION] Completion repair step {next_step_number} completed successfully",
            metadata={"phase": "completion_repair", "step_index": next_step_number},
        )
        return {"status": "success", "step": repair_step}

    orchestration_state.record_failure(step_record)
    task.error_message = assessment.error_message[:2000]
    if session:
        set_session_alert(
            session,
            "error",
            f"Completion repair failed: {assessment.error_message[:1800]}",
        )
    save_orchestration_checkpoint_fn(
        db, ctx.session_id, ctx.task_id, ctx.prompt, orchestration_state
    )
    db.commit()
    emit_live(
        "ERROR",
        f"[ORCHESTRATION] Completion repair step {next_step_number} failed",
        metadata={
            "phase": "completion_repair",
            "step_index": next_step_number,
            "error": assessment.error_message[:1000],
        },
    )
    return {"status": "failed", "reason": assessment.error_message}


def finalize_successful_task(
    *,
    ctx: OrchestrationRunContext,
    write_project_state_snapshot_fn: Callable[..., None] = write_project_state_snapshot,
    save_orchestration_checkpoint_fn: Callable[
        ..., None
    ] = save_orchestration_checkpoint,
    get_next_pending_project_task_fn: Optional[Callable[..., Any]] = None,
    get_latest_session_task_link_fn: Optional[Callable[..., Any]] = None,
    execute_openclaw_task_delay_fn: Optional[Callable[..., Any]] = None,
    build_task_report_payload_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    render_task_report_fn: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    db = ctx.db
    openclaw_service = ctx.openclaw_service
    task_service = ctx.task_service
    session = ctx.session
    project = ctx.project
    task = ctx.task
    session_task_link = ctx.session_task_link
    session_id = ctx.session_id
    task_id = ctx.task_id
    prompt = ctx.prompt
    execution_profile = ctx.execution_profile
    validation_profile = ctx.validation_profile
    runs_in_canonical_baseline = ctx.runs_in_canonical_baseline
    orchestration_state = ctx.orchestration_state
    emit_live = ctx.emit_live
    logger = ctx.logger

    logger.info("[ORCHESTRATION] Phase 5: TASK_SUMMARY - summarizing completion")
    emit_phase_event(
        orchestration_state,
        emit_live,
        level="INFO",
        phase="task_summary",
        message="[ORCHESTRATION] Phase 5: TASK_SUMMARY - summarizing completion",
    )

    from app.services import PromptTemplates

    summary_prompt = PromptTemplates.build_task_summary(
        task_description=prompt,
        plan_summary=json.dumps(orchestration_state.plan, indent=2),
        execution_results_summary=orchestration_state.prior_results_summary(),
        changed_files=orchestration_state.changed_files,
        num_debug_attempts=len(orchestration_state.debug_attempts),
        final_status="success",
        execution_profile=execution_profile,
    )
    summary_result = asyncio.run(
        openclaw_service.execute_task(
            summary_prompt, timeout_seconds=SUMMARY_TIMEOUT_SECONDS
        )
    )

    completion_validation = ValidatorService.validate_task_completion(
        project_dir=orchestration_state.project_dir,
        plan=orchestration_state.plan,
        task_prompt=prompt,
        execution_profile=execution_profile,
        workspace_consistency=task_service.analyze_workspace_consistency(
            orchestration_state.project_dir
        ),
        title=task.title if task else None,
        description=task.description if task else None,
        relaxed_mode=orchestration_state.relaxed_mode,
    )
    record_validation_verdict(
        db,
        session_id,
        task_id,
        orchestration_state,
        completion_validation,
    )
    db.commit()

    if completion_validation.repairable:
        repair_result = _attempt_completion_repair(
            ctx=ctx,
            completion_validation=completion_validation,
            save_orchestration_checkpoint_fn=save_orchestration_checkpoint_fn,
        )
        if repair_result.get("status") == "success":
            completion_validation = ValidatorService.validate_task_completion(
                project_dir=orchestration_state.project_dir,
                plan=orchestration_state.plan,
                task_prompt=prompt,
                execution_profile=execution_profile,
                workspace_consistency=task_service.analyze_workspace_consistency(
                    orchestration_state.project_dir
                ),
                title=task.title if task else None,
                description=task.description if task else None,
                relaxed_mode=orchestration_state.relaxed_mode,
            )
            record_validation_verdict(
                db,
                session_id,
                task_id,
                orchestration_state,
                completion_validation,
            )
            db.commit()
        else:
            completion_error = "Completion repair failed: " + str(
                repair_result.get("reason") or "unknown reason"
            )
            completion_failure_reason = str(
                repair_result.get("reason") or "unknown reason"
            )
            orchestration_state.status = OrchestrationStatus.ABORTED
            orchestration_state.abort_reason = completion_error
            task.status = TaskStatus.FAILED
            task.completed_at = datetime.utcnow()
            task.error_message = completion_error
            task.current_step = len(orchestration_state.plan)
            task.workspace_status = "blocked"
            if session_task_link:
                session_task_link.status = TaskStatus.FAILED
                session_task_link.completed_at = task.completed_at
            if session:
                session.status = "paused"
                session.is_active = False
                set_session_alert(session, "error", completion_error[:2000])
            db.commit()
            emit_live(
                "ERROR",
                f"[ORCHESTRATION] Completion repair failed: {completion_failure_reason}",
                metadata={
                    "phase": "completion_repair",
                    "reason": completion_failure_reason,
                },
            )
            save_orchestration_checkpoint_fn(
                db, session_id, task_id, prompt, orchestration_state
            )
            write_project_state_snapshot_fn(db, project, task, session_id)
            return {"status": "failed", "reason": "completion_repair_failed"}

    if completion_validation.warning:
        emit_live(
            "WARN",
            "[ORCHESTRATION] Task completion passed with validator warnings",
            metadata={
                "phase": "task_validation",
                "validation_status": completion_validation.status,
                "reasons": completion_validation.reasons[:10],
                "relaxed_mode": orchestration_state.relaxed_mode,
            },
        )

    if not completion_validation.accepted:
        completion_error = "Completion validation failed: " + "; ".join(
            completion_validation.reasons[:5]
        )
        orchestration_state.status = OrchestrationStatus.ABORTED
        orchestration_state.abort_reason = completion_error
        task.status = TaskStatus.FAILED
        task.completed_at = datetime.utcnow()
        task.error_message = completion_error
        task.current_step = len(orchestration_state.plan)
        task.workspace_status = "blocked"
        if session_task_link:
            session_task_link.status = TaskStatus.FAILED
            session_task_link.completed_at = task.completed_at
        if session:
            session.status = "paused"
            session.is_active = False
            set_session_alert(session, "error", completion_error[:2000])
        db.commit()
        emit_live(
            "ERROR",
            "[ORCHESTRATION] Task completion failed validation",
            metadata={
                "phase": "task_validation",
                "validation_status": completion_validation.status,
                "profile": completion_validation.profile,
                "reasons": completion_validation.reasons[:10],
            },
        )
        save_orchestration_checkpoint_fn(
            db, session_id, task_id, prompt, orchestration_state
        )
        write_project_state_snapshot_fn(db, project, task, session_id)
        return {"status": "failed", "reason": "completion_validation_failed"}

    baseline_publish_result = None
    baseline_publish_validation = None
    if project and task.task_subfolder and not runs_in_canonical_baseline:
        baseline_publish_result = task_service.auto_publish_task_into_baseline(
            project, task
        )
        baseline_materialization = task_service.validate_task_baseline_materialization(
            project, task
        )
        baseline_overview = task_service.validate_project_baseline(
            project, current_task=task
        )
        baseline_publish_validation = ValidatorService.validate_baseline_publish(
            validation_profile=validation_profile,
            baseline_path=baseline_materialization.get("baseline_path") or "",
            baseline_file_count=baseline_materialization.get("baseline_file_count", 0),
            missing_task_expected_files=baseline_materialization.get(
                "missing_expected_files", []
            ),
            missing_prior_expected_files=baseline_overview.get(
                "missing_expected_files", []
            ),
            consistency_issues=baseline_materialization.get("consistency_issues", []),
            consistency_details=baseline_materialization.get("consistency"),
            relaxed_mode=orchestration_state.relaxed_mode,
        )
        record_validation_verdict(
            db,
            session_id,
            task_id,
            orchestration_state,
            baseline_publish_validation,
        )
        db.commit()
        if baseline_publish_validation.warning:
            emit_live(
                "WARN",
                "[ORCHESTRATION] Baseline publish passed with validator warnings",
                metadata={
                    "phase": "baseline_publish",
                    "validation_status": baseline_publish_validation.status,
                    "reasons": baseline_publish_validation.reasons[:10],
                    "relaxed_mode": orchestration_state.relaxed_mode,
                },
            )

        if not baseline_publish_validation.accepted:
            baseline_error = "Baseline publish validation failed: " + "; ".join(
                baseline_publish_validation.reasons[:5]
            )
            orchestration_state.status = OrchestrationStatus.ABORTED
            orchestration_state.abort_reason = baseline_error
            task.status = TaskStatus.FAILED
            task.completed_at = datetime.utcnow()
            task.error_message = baseline_error
            task.current_step = len(orchestration_state.plan)
            task.workspace_status = "blocked"
            if session_task_link:
                session_task_link.status = TaskStatus.FAILED
                session_task_link.completed_at = task.completed_at
            if session:
                session.status = "paused"
                session.is_active = False
                set_session_alert(session, "error", baseline_error[:2000])
            db.commit()
            emit_live(
                "ERROR",
                "[ORCHESTRATION] Baseline publish failed validation",
                metadata={
                    "phase": "baseline_publish",
                    "validation_status": baseline_publish_validation.status,
                    "reasons": baseline_publish_validation.reasons[:10],
                },
            )
            save_orchestration_checkpoint_fn(
                db, session_id, task_id, prompt, orchestration_state
            )
            write_project_state_snapshot_fn(db, project, task, session_id)
            return {
                "status": "failed",
                "reason": "baseline_publish_validation_failed",
            }

    task.status = TaskStatus.DONE
    task.completed_at = datetime.utcnow()
    task.error_message = None
    task.summary = summary_result.get("output", "")[:2000]
    task.current_step = len(orchestration_state.plan)
    task.workspace_status = "ready" if task.task_subfolder else "not_created"
    if session_task_link:
        session_task_link.status = TaskStatus.DONE
        session_task_link.completed_at = task.completed_at

    set_session_alert(session, None, None)

    next_task = None
    blocked_pending_task = None
    if (
        session
        and session.execution_mode == "automatic"
        and get_next_pending_project_task_fn
    ):
        next_task = get_next_pending_project_task_fn(db, session.project_id)
        if not next_task and session.project_id:
            blocked_pending_task = (
                db.query(Task)
                .filter(
                    Task.project_id == session.project_id,
                    Task.status == TaskStatus.PENDING,
                )
                .order_by(
                    Task.plan_position.asc().nullslast(),
                    Task.priority.desc(),
                    Task.created_at.asc().nullslast(),
                    Task.id.asc(),
                )
                .first()
            )

    if session:
        if next_task:
            session.status = "running"
            session.is_active = True
        elif blocked_pending_task:
            session.status = "paused"
            session.is_active = False
            blockers = type(task_service)(db).get_blocking_prior_tasks(
                blocked_pending_task
            )
            if blockers:
                blocking_summary = ", ".join(
                    f"#{item.plan_position} {item.title} ({item.status.value})"
                    for item in blockers[:3]
                )
                set_session_alert(
                    session,
                    "warning",
                    (
                        "Automatic execution is paused because an earlier ordered task "
                        f"is incomplete: {blocking_summary}"
                    )[:2000],
                )
        else:
            session.status = "stopped"
            session.is_active = False

    db.commit()
    write_project_state_snapshot_fn(db, project, task, session_id)

    logger.info(
        "[ORCHESTRATION] Task %s completed successfully with %s steps",
        task_id,
        len(orchestration_state.plan),
    )
    emit_live(
        "INFO",
        f"[ORCHESTRATION] Task {task_id} completed successfully with {len(orchestration_state.plan)} steps",
        metadata={
            "phase": "completed",
            "steps": len(orchestration_state.plan),
            "baseline_publish_result": baseline_publish_result,
        },
    )

    if baseline_publish_result:
        db.add(
            LogEntry(
                session_id=session_id,
                session_instance_id=session.instance_id,
                task_id=task_id,
                level="INFO",
                message=(
                    "[ORCHESTRATION] Published task workspace into canonical project baseline "
                    f"({baseline_publish_result.get('files_copied', 0)} files)"
                ),
                log_metadata=json.dumps(baseline_publish_result),
            )
        )
        db.commit()

    if (
        session
        and next_task
        and get_latest_session_task_link_fn
        and execute_openclaw_task_delay_fn
    ):
        next_session_task_link = get_latest_session_task_link_fn(
            db, session_id, next_task.id
        )
        if not next_session_task_link:
            next_session_task_link = SessionTask(
                session_id=session_id,
                task_id=next_task.id,
                status=TaskStatus.RUNNING,
                started_at=datetime.utcnow(),
            )
            db.add(next_session_task_link)
        else:
            next_session_task_link.status = TaskStatus.RUNNING
            next_session_task_link.started_at = datetime.utcnow()
            next_session_task_link.completed_at = None

        next_task.status = TaskStatus.RUNNING
        next_task.started_at = datetime.utcnow()
        next_task.completed_at = None
        next_task.error_message = None
        next_task.current_step = 0

        db.add(
            LogEntry(
                session_id=session_id,
                session_instance_id=session.instance_id,
                task_id=next_task.id,
                level="INFO",
                message=(
                    f"[ORCHESTRATION] Auto-advancing to next task {next_task.id}: {next_task.title}"
                ),
                log_metadata=json.dumps(
                    {
                        "auto_advance": True,
                        "plan_position": getattr(next_task, "plan_position", None),
                    }
                ),
            )
        )
        db.commit()
        execute_openclaw_task_delay_fn(
            session_id=session_id,
            task_id=next_task.id,
            prompt=next_task.description or next_task.title,
            timeout_seconds=900,
        )

    if build_task_report_payload_fn and render_task_report_fn:
        try:
            report_payload = build_task_report_payload_fn(db, task_id)
            report_result = render_task_report_fn(
                report_payload, output_format="markdown"
            )
            if report_result and "report" in report_result:
                report_content = report_result["report"]
                report_filename = f"task_report_{task_id}.md"
                report_path = orchestration_state.project_dir / report_filename
                os.makedirs(orchestration_state.project_dir, exist_ok=True)
                with open(report_path, "w", encoding="utf-8") as handle:
                    handle.write(report_content)
                logger.info("[REPORT] Task report saved to: %s", report_path)
        except Exception as report_error:
            logger.error(
                "[REPORT] Failed to generate task report: %s", str(report_error)
            )

    return {
        "status": "completed",
        "task_id": task_id,
        "session_id": session_id,
        "steps_completed": len(orchestration_state.plan),
        "debug_attempts": len(orchestration_state.debug_attempts),
        "summary": summary_result.get("output", "")[:500],
    }
