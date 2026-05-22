"""Execution/debug loop for step-by-step orchestration."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os as _os
import re as _re
import shlex
import subprocess as _subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from app.models import TaskExecution, TaskStatus
from app.services.agents.interfaces import RuntimeBackendResult
from app.services.orchestration.context.assembly import (
    DebugPromptInputs,
    assemble_debugging_prompt,
    assemble_execution_prompt,
    assemble_plan_revision_prompt,
)
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.events.telemetry import emit_phase_event
from app.services.orchestration.diagnostics.debug_feedback import (
    build_bounded_debug_repair_prompt,
    build_debug_feedback_envelope,
    normalize_bounded_debug_repair_payload,
    persist_debug_feedback_envelope,
)
from app.services.orchestration.diagnostics.diff_capsule import (
    build_bounded_diff_repair_prompt,
    build_diff_capsule,
    snapshot_file_contents,
)
from app.services.orchestration.diagnostics.evidence_capsule import (
    collect_workspace_evidence,
)
from app.services.orchestration.execution import ExecutorService
from app.services.orchestration.execution.execution_flow import (
    assess_step_execution,
    determine_step_timeout,
    execute_verification_command,
    repeated_tool_path_failure_decision,
)
from app.services.orchestration.execution.step_support import (
    coerce_debug_step_result,
    coerce_execution_step_result,
    is_runnable_shell_command_fix,
    repair_step_commands_with_self_correction,
    step_needs_command_repair,
)
from app.services.orchestration.policy import (
    DEBUG_TIMEOUT_SECONDS,
    MAX_STEP_ATTEMPTS,
)
from app.services.orchestration.run_state import (
    mark_task_attempt_cancelled,
    mark_task_attempt_failed,
)
from app.services.orchestration.state.persistence import (
    append_orchestration_event,
    attach_failure_envelope,
    emit_intent_outcome_mismatch,
    maybe_emit_divergence_detected,
    read_orchestration_events,
    record_validation_verdict,
    save_orchestration_checkpoint,
    write_orchestration_state_snapshot,
)
from app.services.orchestration.state.session_state import mark_session_paused
from app.services.orchestration.types import (
    FailureEnvelope,
    OrchestrationRunContext,
    classify_failure_root_cause,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.services.orchestration.validation.parsing import (
    build_json_compliance_retry_prompt,
)
from app.services.orchestration.validation.workspace_guard import (
    TaskOperationContractViolation,
    TaskWorkspaceViolationError,
    assert_no_workspace_cd_escape,
    compute_workspace_checksum,
    detect_scope_violations,
    normalize_path_reference,
    summarize_step_changes,
)
from app.services.orchestration.context.hitl_sentinel import (
    parse as _parse_hitl_sentinel,
)
from app.services.prompt_templates import OrchestrationStatus, StepResult


def _run_coroutine(coro: Any) -> Any:
    # asyncio.run() deadlocks inside a Celery ForkPoolWorker because os.fork()
    # inherits Python's asyncio internal mutexes in a locked state from the
    # parent process. Running in a fresh ThreadPoolExecutor thread avoids this:
    # the thread is not forked, so it starts with a clean event loop state.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _executor:
        return _executor.submit(asyncio.run, coro).result()


def _get_task_execution(
    db: Any, task_execution_id: Optional[int]
) -> Optional[TaskExecution]:
    if task_execution_id is None:
        return None
    return db.query(TaskExecution).filter(TaskExecution.id == task_execution_id).first()


def _normalize_runtime_execution_result(
    runtime_service: Any,
    result: Dict[str, Any],
    *,
    duration_seconds: float,
) -> RuntimeBackendResult | None:
    normalizer = getattr(runtime_service, "normalize_execution_result", None)
    if not callable(normalizer):
        return None
    return normalizer(
        result,
        role="execution",
        duration_seconds=duration_seconds,
    )


def _persist_runtime_backend_result(
    db: Any,
    task_execution_id: Optional[int],
    result: RuntimeBackendResult | None,
) -> None:
    """Persist normalized backend metadata for the active execution attempt."""

    if task_execution_id is None or result is None:
        return
    task_execution = _get_task_execution(db, task_execution_id)
    if task_execution is None:
        return
    task_execution.backend_id = result.backend_id
    if not result.success and result.failure_category:
        task_execution.failure_category = result.failure_category
    db.flush()


def _verification_can_replace_stale_commands(step: dict[str, Any]) -> bool:
    commands = step.get("commands")
    if not isinstance(commands, list) or not commands:
        return False
    if isinstance(step.get("ops"), list) and step.get("ops"):
        return False
    return True


def _is_read_only_inspection_command(command: str) -> bool:
    normalized = " ".join(str(command or "").strip().split())
    if not normalized:
        return False
    allowed_prefixes = (
        "ls",
        "find .",
        "rg --files",
        "cat ",
        "pwd",
        "true",
    )
    if not normalized.startswith(allowed_prefixes):
        return False
    blocked_tokens = (" >", ">>", "&&", ";", "||", "$(", "`")
    if any(token in normalized for token in blocked_tokens):
        if normalized == "rg --files . | sort":
            return True
        return False
    if normalized == "rg --files . | sort":
        return True
    if normalized.startswith("find .") and "| head" in normalized:
        return True
    return "|" not in normalized


def _is_simple_verification_command(command: str) -> bool:
    normalized = " ".join(str(command or "").strip().split())
    if not normalized:
        return False
    if normalized.startswith(("python -c ", "python3 -c ")):
        return _python_inline_verification_script(normalized) is not None
    if normalized.startswith("node -e "):
        return _node_eval_script(normalized) is not None
    allowed_prefixes = (
        "python -m py_compile ",
        "python3 -m py_compile ",
        "python -m unittest ",
        "python3 -m unittest ",
        "npm run build",
        "pytest",
        "python -m pytest",
        "python3 -m pytest",
    )
    if not normalized.startswith(allowed_prefixes):
        return False
    return not any(
        token in normalized for token in (" >", ">>", ";", "&&", "||", "$(", "`")
    )


def _python_inline_verification_script(command: str) -> str | None:
    normalized = " ".join(str(command or "").strip().split())
    try:
        tokens = shlex.split(normalized, posix=True)
    except ValueError:
        return None
    if len(tokens) != 3 or tokens[0] not in {"python", "python3"} or tokens[1] != "-c":
        return None

    script = tokens[2]
    lowered = script.lower()
    blocked_fragments = (
        "__",
        "open(",
        ".write(",
        ".unlink(",
        ".rename(",
        ".replace(",
        "remove(",
        "rmdir(",
        "mkdir(",
        "shutil",
        "subprocess",
        "socket",
        "requests",
        "urllib",
        "http://",
        "https://",
        "os.",
        "sys.argv",
        "eval(",
        "exec(",
    )
    if any(fragment in lowered for fragment in blocked_fragments):
        return None
    path_check = "pathlib.path(" in lowered and any(
        fragment in lowered for fragment in (".read_text(", ".exists(")
    )
    config_check = "configparser" in lowered and ".read(" in lowered
    module_assert_check = "import utils" in lowered and "assert " in lowered
    if not (path_check or config_check or module_assert_check):
        return None
    if not any(fragment in lowered for fragment in ("sys.exit(", "print(")):
        if not module_assert_check:
            return None
    if module_assert_check and "assert " not in lowered:
        return None
    return script


def _patch_python_verification_cmd(command: str) -> str:
    """Prepend 'import sys; ' when a python -c script uses sys.* without importing it."""
    normalized = " ".join(str(command or "").strip().split())
    if not normalized.startswith(("python -c ", "python3 -c ")):
        return command
    try:
        tokens = shlex.split(normalized, posix=True)
    except ValueError:
        return command
    if len(tokens) != 3:
        return command
    script = tokens[2]
    if "sys." in script and "import sys" not in script:
        script = "import sys; " + script
        return f"{tokens[0]} -c {shlex.quote(script)}"
    return command


def _node_eval_script(command: str) -> str | None:
    normalized = " ".join(str(command or "").strip().split())
    if not normalized.startswith("node -e "):
        return None
    try:
        tokens = shlex.split(normalized, posix=True)
    except ValueError:
        tokens = []
    if len(tokens) == 3 and tokens[0] == "node" and tokens[1] == "-e":
        script = tokens[2]
    else:
        script = normalized[len("node -e ") :].strip()
        if len(script) >= 2 and script[0] == script[-1] and script[0] in {"'", '"'}:
            script = script[1:-1]
    return script.replace('\\\\"', '"').replace('\\"', '"')


def _same_simple_verification_command(command: str, verification: str) -> bool:
    if command == verification:
        return True
    try:
        if shlex.split(command, posix=True) == shlex.split(verification, posix=True):
            return True
    except ValueError:
        pass
    command_script = _node_eval_script(command)
    verification_script = _node_eval_script(verification)
    return bool(command_script and command_script == verification_script)


def _execute_read_only_inspection_step(
    *,
    project_dir: Path,
    commands: list[Any],
) -> dict[str, Any] | None:
    if not commands or not all(
        _is_read_only_inspection_command(str(c)) for c in commands
    ):
        return None

    outputs: list[str] = []
    for command in commands:
        result = execute_verification_command(
            project_dir=project_dir,
            command=str(command),
            timeout_seconds=30,
        )
        if not result.get("success"):
            return {
                "status": "failed",
                "output": result.get("output", ""),
                "error": result.get("output", "read-only inspection command failed"),
                "files_changed": [],
            }
        command_output = str(result.get("output") or "").strip()
        if command_output:
            outputs.append(f"$ {command}\n{command_output}")
    return {
        "status": "completed",
        "output": "\n\n".join(outputs),
        "verification_output": "",
        "skip_declared_verification": True,
        "files_changed": [],
    }


def _is_safe_local_shell_command(command: str) -> bool:
    """Return True for shell commands safe to run locally in the project workspace.

    Handles common file-creation patterns the LLM generates instead of write_file
    ops — echo redirects, mkdir, touch. These would otherwise fall through to the
    LLM execute_task path where no local file is actually created.
    """
    normalized = " ".join(str(command or "").strip().split())
    if not normalized:
        return False
    blocked_tokens = (
        "$(",
        "`",
        "curl",
        "wget",
        "pip",
        "npm",
        "yarn",
        "apt",
        "yum",
        "brew",
        "rm ",
        "rm\t",
        "sudo",
        "; rm",
        "&&rm",
        "||rm",
        "..",  # block path traversal
    )
    if any(t in normalized for t in blocked_tokens):
        return False
    safe_prefixes = (
        "echo ",
        "echo\t",
        "printf ",
        "mkdir ",
        "mkdir\t",
        "touch ",
        "cp ",
        "cp\t",
        "mv ",
        "mv\t",
        "chmod ",
        "chmod\t",
    )
    return any(normalized.startswith(p) for p in safe_prefixes)


def _is_workspace_local_path_token(token: str, project_dir: Path) -> bool:
    raw = str(token or "").strip()
    if not raw:
        return False
    if _re.match(r"^[A-Za-z]:[\\/]", raw) or raw.startswith(("\\\\", "//")):
        return False
    try:
        normalize_path_reference(raw, project_dir)
    except TaskWorkspaceViolationError:
        return False
    return True


def _local_shell_command_paths_are_safe(command: str, project_dir: Path) -> bool:
    if _re.search(r"(^|[\s>])([A-Za-z]:[\\/]|\\\\)", str(command or "")):
        return False
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return False
    if not tokens:
        return False

    executable = tokens[0]
    if executable in {"echo", "printf"}:
        index = 1
        while index < len(tokens):
            token = tokens[index]
            if token in {">", ">>"}:
                if index + 1 >= len(tokens):
                    return False
                if not _is_workspace_local_path_token(tokens[index + 1], project_dir):
                    return False
                index += 2
                continue
            if token.startswith((">", ">>")):
                target = token[2:] if token.startswith(">>") else token[1:]
                if not _is_workspace_local_path_token(target, project_dir):
                    return False
            index += 1
        return True

    if executable in {"mkdir", "touch"}:
        operands = [token for token in tokens[1:] if not token.startswith("-")]
        return bool(operands) and all(
            _is_workspace_local_path_token(token, project_dir) for token in operands
        )

    if executable in {"cp", "mv"}:
        operands = [token for token in tokens[1:] if not token.startswith("-")]
        return len(operands) >= 2 and all(
            _is_workspace_local_path_token(token, project_dir) for token in operands
        )

    if executable == "chmod":
        operands = [token for token in tokens[1:] if not token.startswith("-")]
        if len(operands) < 2:
            return False
        mode = operands[0]
        if not _re.match(r"^(?:[0-7]{3,4}|[ugoa]*[+-=][rwxXstugo,+-=]+)$", mode):
            return False
        paths = operands[1:]
        return all(
            _is_workspace_local_path_token(token, project_dir) for token in paths
        )

    return False


def _execute_local_shell_commands_step(
    *,
    project_dir: Path,
    commands: list[Any],
    verification_command: Any,
) -> dict[str, Any] | None:
    """Execute safe shell write commands locally instead of delegating to the LLM.

    Returns None if any command fails the safety check, so the caller can fall
    through to the LLM path.
    """
    normalized_cmds = [
        str(c or "").strip() for c in (commands or []) if str(c or "").strip()
    ]
    if not normalized_cmds:
        return None
    if not all(_is_safe_local_shell_command(c) for c in normalized_cmds):
        return None
    # Workspace escape check
    for cmd in normalized_cmds:
        try:
            assert_no_workspace_cd_escape(cmd, project_dir)
        except TaskWorkspaceViolationError:
            return None
        if not _local_shell_command_paths_are_safe(cmd, project_dir):
            return None

    files_before = set(
        _os.path.relpath(_os.path.join(root, f), project_dir)
        for root, _, files in _os.walk(project_dir)
        for f in files
    )
    outputs: list[str] = []
    for cmd in normalized_cmds:
        try:
            result = _subprocess.run(
                cmd,
                cwd=str(project_dir),
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except _subprocess.TimeoutExpired:
            return {
                "status": "failed",
                "output": f"Command timed out: {cmd}",
                "error": "timeout",
                "files_changed": [],
            }
        if result.returncode != 0:
            err = "\n".join(
                filter(None, [result.stdout.strip(), result.stderr.strip()])
            )
            return {
                "status": "failed",
                "output": err,
                "error": err,
                "files_changed": [],
            }
        out = "\n".join(filter(None, [result.stdout.strip(), result.stderr.strip()]))
        if out:
            outputs.append(f"$ {cmd}\n{out}")

    files_after = set(
        _os.path.relpath(_os.path.join(root, f), project_dir)
        for root, _, files in _os.walk(project_dir)
        for f in files
    )
    files_changed = [
        f for f in files_after - files_before if not f.startswith(".openclaw")
    ]

    # Run verification if provided
    verification = _patch_python_verification_cmd(
        str(verification_command or "").strip()
    )
    if verification:
        vresult = execute_verification_command(
            project_dir=project_dir,
            command=verification,
            timeout_seconds=60,
        )
        if not vresult.get("success"):
            verr = vresult.get("output", "verification failed")
            return {
                "status": "failed",
                "output": verr,
                "error": verr,
                "files_changed": files_changed,
            }

    return {
        "status": "completed",
        "output": "\n\n".join(outputs),
        "verification_output": "",
        "files_changed": files_changed,
    }


def _execute_simple_verification_step(
    *,
    project_dir: Path,
    commands: list[Any],
    verification_command: Any,
) -> dict[str, Any] | None:
    normalized_commands = [str(command or "").strip() for command in commands or []]
    verification = str(verification_command or "").strip()
    if len(normalized_commands) != 1 or not verification:
        return None
    command = normalized_commands[0]
    command_is_simple_verification = _is_simple_verification_command(command)
    verification_is_simple = _is_simple_verification_command(verification)
    if _same_simple_verification_command(command, verification):
        command_to_run = verification
    elif (
        command.startswith("node -e ")
        and verification.startswith("node -e ")
        and command_is_simple_verification
        and verification_is_simple
    ):
        command_to_run = command
    elif command_is_simple_verification and verification_is_simple:
        command_to_run = command
    else:
        return None
    command_to_run = _patch_python_verification_cmd(command_to_run)
    if not _is_simple_verification_command(command_to_run):
        return None

    result = execute_verification_command(
        project_dir=project_dir,
        command=command_to_run,
        timeout_seconds=120,
    )
    return {
        "status": "completed" if result.get("success") else "failed",
        "output": result.get("output", ""),
        "verification_output": result.get("output", ""),
        "error": "" if result.get("success") else result.get("output", ""),
        "files_changed": [],
    }


def execute_step_loop(
    *,
    ctx: OrchestrationRunContext,
    extract_structured_text: Callable[[Any], str],
    normalize_step: Callable[..., Dict[str, Any]],
    normalize_plan_with_live_logging: Callable[..., Any],
    workspace_violation_error_cls: type[Exception],
    write_project_state_snapshot_fn: Callable[..., None],
    record_live_log_fn: Callable[..., None],
) -> Dict[str, Any]:
    db = ctx.db
    session = ctx.session
    project = ctx.project
    task = ctx.task
    session_task_link = ctx.session_task_link
    session_id = ctx.session_id
    task_id = ctx.task_id
    prompt = ctx.prompt
    timeout_seconds = ctx.timeout_seconds
    execution_profile = ctx.execution_profile
    validation_profile = ctx.validation_profile
    orchestration_state = ctx.orchestration_state
    runtime_service = ctx.runtime_service
    task_service = ctx.task_service
    logger = ctx.logger
    emit_live = ctx.emit_live
    error_handler = ctx.error_handler
    restore_workspace_snapshot_if_needed = ctx.restore_workspace_snapshot_if_needed

    # Synthesize a minimal artifact when resuming from an old checkpoint that
    # predates the reasoning_artifact field (stored None).
    if getattr(orchestration_state, "reasoning_artifact", None) is None:
        orchestration_state.reasoning_artifact = {
            "schema_version": 1,
            "intent": " ".join(str(ctx.prompt or "").split())[:220],
            "workspace_facts": [f"project_dir={orchestration_state.project_dir}"],
            "planned_actions": [
                str(step.get("description") or f"Step {i + 1}")
                for i, step in enumerate(orchestration_state.plan[:8])
            ],
            "verification_plan": ["verify each planned step outcome"],
        }

    reasoning_verdict = ValidatorService.validate_reasoning_artifact(
        getattr(orchestration_state, "reasoning_artifact", None),
        plan=orchestration_state.plan,
        validation_severity=ctx.validation_severity,
    )
    if not reasoning_verdict.accepted:
        orchestration_state.status = OrchestrationStatus.ABORTED
        orchestration_state.abort_reason = (
            "Structured reasoning artifact is missing or invalid for execution"
        )
        record_validation_verdict(
            db,
            session_id,
            task_id,
            orchestration_state,
            reasoning_verdict,
        )
        error_message = (
            "Execution blocked before step 1 because the reasoning artifact is invalid: "
            + "; ".join(reasoning_verdict.reasons[:4])
        )
        mark_task_attempt_failed(
            task=task,
            session_task_link=session_task_link,
            task_execution=_get_task_execution(db, ctx.task_execution_id),
            error_message=error_message,
            completed_at=datetime.now(timezone.utc),
        )
        if session:
            mark_session_paused(
                session, alert_level="error", alert_message=error_message[:2000]
            )
        db.commit()
        restore_workspace_snapshot_if_needed("reasoning artifact gate failed")
        write_project_state_snapshot_fn(db, project, task, session_id)
        return {"status": "failed", "reason": "reasoning_artifact_gate_failed"}

    orchestration_state.status = OrchestrationStatus.EXECUTING
    logger.info(
        "[ORCHESTRATION] Phase 2: EXECUTING - executing %s steps",
        len(orchestration_state.plan),
    )
    emit_phase_event(
        orchestration_state,
        emit_live,
        level="INFO",
        phase="executing",
        message=f"[ORCHESTRATION] Phase 2: EXECUTING - executing {len(orchestration_state.plan)} steps",
        details={"steps": len(orchestration_state.plan)},
    )
    executing_phase_event = None
    try:
        executing_phase_event = append_orchestration_event(
            project_dir=orchestration_state.project_dir,
            session_id=session_id,
            task_id=task_id,
            event_type=EventType.PHASE_STARTED,
            details={"phase": "executing", "steps": len(orchestration_state.plan)},
        )
        write_orchestration_state_snapshot(
            project_dir=orchestration_state.project_dir,
            session_id=session_id,
            task_id=task_id,
            orchestration_state=orchestration_state,
            trigger="phase_started",
            related_event_id=executing_phase_event.get("event_id"),
        )
    except Exception:
        pass

    # Stop check is at step boundaries only. A step already in progress (e.g. a
    # long LLM call) runs to completion even after revoke_session_celery_tasks
    # fires. SIGTERM from terminate=True may also be delayed if the worker is
    # mid-HTTP-request. The pause is "guaranteed before the next step" not
    # "instantaneous".
    plan_revision_count = 0
    while orchestration_state.current_step_index < len(orchestration_state.plan):
        step_index = orchestration_state.current_step_index
        step = orchestration_state.plan[step_index]
        db.refresh(session)
        if (
            session.status in ["stopped", "paused", "awaiting_input"]
            or not session.is_active
        ):
            logger.info(
                "[ORCHESTRATION] Session %s marked %s; stopping task execution before step %s",
                session_id,
                session.status,
                step_index + 1,
            )
            save_orchestration_checkpoint(
                db, session_id, task_id, prompt, orchestration_state
            )
            task_execution = (
                db.query(TaskExecution)
                .filter(TaskExecution.id == ctx.task_execution_id)
                .first()
                if ctx.task_execution_id
                else None
            )
            mark_task_attempt_cancelled(
                task=task,
                session_task_link=session_task_link,
                task_execution=task_execution,
                completed_at=datetime.now(timezone.utc),
            )
            db.commit()
            restore_workspace_snapshot_if_needed(f"session {session.status}")
            write_project_state_snapshot_fn(db, project, task, session_id)
            return {
                "status": "cancelled",
                "task_id": task_id,
                "session_id": session_id,
                "reason": f"session_{session.status}",
            }

        orchestration_state.current_step_index = step_index
        task.current_step = step_index + 1
        save_orchestration_checkpoint(
            db, session_id, task_id, prompt, orchestration_state
        )
        db.commit()

        step_description = step.get("description", f"Step {step_index + 1}")
        step_commands = step.get("commands", [])
        step_ops = step.get("ops", [])
        verification_command = step.get("verification")
        rollback_command = step.get("rollback")
        expected_files = step.get("expected_files", [])

        if step_needs_command_repair(step):
            repaired_step = None
            for repair_attempt in range(1, 3):
                emit_live(
                    "WARN",
                    (
                        f"[ORCHESTRATION] Step {step_index + 1} has no runnable commands; "
                        f"attempting self-correction ({repair_attempt}/2)"
                    ),
                    metadata={
                        "phase": "step_validation",
                        "step_index": step_index + 1,
                        "attempt": repair_attempt,
                    },
                )
                repaired_step = repair_step_commands_with_self_correction(
                    runtime_service=runtime_service,
                    db=db,
                    session_id=session_id,
                    task_id=task_id,
                    session_instance_id=session.instance_id if session else None,
                    task_prompt=prompt,
                    step=step,
                    step_index=step_index,
                    project_dir=orchestration_state.project_dir,
                    prior_results_summary=orchestration_state.prior_results_summary(),
                    project_context=orchestration_state.project_context,
                    logger_obj=logger,
                    extract_structured_text=extract_structured_text,
                    normalize_step=normalize_step,
                    record_live_log=record_live_log_fn,
                    failure_envelope=FailureEnvelope(
                        session_id=session_id,
                        task_id=task_id,
                        phase="step_validation",
                        step_index=step_index + 1,
                        root_cause="malformed_prompt_output",
                        input={"step": dict(step or {})},
                        output={},
                    ),
                )
                if repaired_step is not None:
                    orchestration_state.plan[step_index] = repaired_step
                    step = repaired_step
                    task.steps = json.dumps(orchestration_state.plan)
                    save_orchestration_checkpoint(
                        db, session_id, task_id, prompt, orchestration_state
                    )
                    db.commit()
                    break

            if repaired_step is None:
                manual_gate_message = (
                    f"Step {step_index + 1} generated empty or invalid commands twice. "
                    "Manual review is required before execution can continue."
                )
                orchestration_state.status = OrchestrationStatus.ABORTED
                orchestration_state.abort_reason = manual_gate_message
                mark_task_attempt_failed(
                    task=task,
                    session_task_link=session_task_link,
                    task_execution=_get_task_execution(db, ctx.task_execution_id),
                    error_message=manual_gate_message,
                    completed_at=datetime.now(timezone.utc),
                )
                mark_session_paused(
                    session,
                    alert_level="error",
                    alert_message=manual_gate_message,
                )
                db.commit()
                restore_workspace_snapshot_if_needed("manual review gate")
                write_project_state_snapshot_fn(db, project, task, session_id)
                return {"status": "failed", "reason": "manual_review_required"}

            step_description = step.get("description", f"Step {step_index + 1}")
            step_commands = step.get("commands", [])
            step_ops = step.get("ops", [])
            verification_command = step.get("verification")
            rollback_command = step.get("rollback")
            expected_files = step.get("expected_files", [])
        scope_violations: list = []
        pre_step_checksum: dict = {}
        pre_step_file_snapshot: dict = {}

        logger.info(
            "[ORCHESTRATION] Executing step %s/%s: %s...",
            step_index + 1,
            len(orchestration_state.plan),
            step_description[:80],
        )
        emit_phase_event(
            orchestration_state,
            emit_live,
            level="INFO",
            phase="executing",
            message=f"[ORCHESTRATION] Executing step {step_index + 1}/{len(orchestration_state.plan)}: {step_description}",
            details={
                "step_index": step_index + 1,
                "step_total": len(orchestration_state.plan),
            },
        )
        step_started_event = None
        try:
            step_started_event = append_orchestration_event(
                project_dir=orchestration_state.project_dir,
                session_id=session_id,
                task_id=task_id,
                event_type=EventType.STEP_STARTED,
                parent_event_id=(executing_phase_event or {}).get("event_id"),
                details={
                    "step_index": step_index + 1,
                    "step_total": len(orchestration_state.plan),
                    "description": step_description[:240],
                },
            )
        except Exception:
            pass

        logger.info(
            "[ORCHESTRATION] Step data: ops=%s, commands=%s, verification=%s",
            step_ops,
            step_commands,
            verification_command,
        )

        # Pre-task audit: snapshot workspace before the step runs
        pre_step_checksum = compute_workspace_checksum(orchestration_state.project_dir)
        pre_step_file_snapshot = snapshot_file_contents(
            orchestration_state.project_dir, expected_files
        )

        ops_result = ExecutorService.execute_file_ops(
            Path(orchestration_state.project_dir), step_ops
        )
        if not ops_result.get("success", False):
            step_result = {
                "status": "failed",
                "output": ops_result.get("output", ""),
                "error": ops_result.get("output", "write_file operation failed"),
                "files_changed": ops_result.get("files_changed", []),
            }
            step_started_at = datetime.now(timezone.utc)
        else:
            if ops_result.get("files_changed"):
                emit_live(
                    "INFO",
                    (
                        f"[ORCHESTRATION] Applied {len(ops_result['files_changed'])} "
                        "structured file operation(s)"
                    ),
                    metadata={
                        "phase": "executing",
                        "step_index": step_index + 1,
                        "ops": "write_file",
                        "files_changed": ops_result["files_changed"][:20],
                    },
                )

        step_started_at = datetime.now(timezone.utc)
        if ops_result.get("success", False):
            if any(str(command or "").strip() for command in (step_commands or [])):
                local_inspection_result = _execute_read_only_inspection_step(
                    project_dir=orchestration_state.project_dir,
                    commands=step_commands,
                )
                if local_inspection_result is not None:
                    step_result = local_inspection_result
                else:
                    local_verification_result = _execute_simple_verification_step(
                        project_dir=orchestration_state.project_dir,
                        commands=step_commands,
                        verification_command=verification_command,
                    )
                    if local_verification_result is not None:
                        step_result = local_verification_result
                    else:
                        local_shell_result = _execute_local_shell_commands_step(
                            project_dir=orchestration_state.project_dir,
                            commands=step_commands,
                            verification_command=verification_command,
                        )
                        if local_shell_result is not None:
                            step_result = local_shell_result
                        else:
                            execution_prompt = assemble_execution_prompt(ctx, step)
                            step_timeout_seconds = determine_step_timeout(
                                timeout_seconds=timeout_seconds,
                                total_steps=len(orchestration_state.plan),
                                execution_profile=execution_profile,
                                step_description=step_description,
                                task_prompt=prompt,
                            )
                            runtime_started_at = time.monotonic()
                            step_result = _run_coroutine(
                                runtime_service.execute_task(
                                    execution_prompt,
                                    timeout_seconds=step_timeout_seconds,
                                )
                            )
                            runtime_backend_result = (
                                _normalize_runtime_execution_result(
                                    runtime_service,
                                    step_result,
                                    duration_seconds=time.monotonic()
                                    - runtime_started_at,
                                )
                            )
                            if runtime_backend_result is not None:
                                _persist_runtime_backend_result(
                                    db,
                                    ctx.task_execution_id,
                                    runtime_backend_result,
                                )
                                step_result["_runtime_backend_result"] = (
                                    runtime_backend_result.to_dict()
                                )
                            if runtime_service.reports_context_overflow(step_result):
                                logger.warning(
                                    "[ORCHESTRATION] Execution prompt exceeded context window at step %s; "
                                    "retrying with compact prompt",
                                    step_index + 1,
                                )
                                emit_live(
                                    "WARN",
                                    f"[ORCHESTRATION] Step {step_index + 1} execution prompt exceeded context window; retrying compact",
                                    metadata={
                                        "phase": "executing",
                                        "step_index": step_index + 1,
                                        "compact_retry": True,
                                    },
                                )
                                compact_execution_prompt = assemble_execution_prompt(
                                    ctx, step, compact=True
                                )
                                runtime_started_at = time.monotonic()
                                step_result = _run_coroutine(
                                    runtime_service.execute_task(
                                        compact_execution_prompt,
                                        timeout_seconds=step_timeout_seconds,
                                    )
                                )
                                runtime_backend_result = (
                                    _normalize_runtime_execution_result(
                                        runtime_service,
                                        step_result,
                                        duration_seconds=time.monotonic()
                                        - runtime_started_at,
                                    )
                                )
                                if runtime_backend_result is not None:
                                    _persist_runtime_backend_result(
                                        db,
                                        ctx.task_execution_id,
                                        runtime_backend_result,
                                    )
                                    step_result["_runtime_backend_result"] = (
                                        runtime_backend_result.to_dict()
                                    )
            else:
                step_result = {
                    "status": "completed",
                    "output": ops_result.get("output", ""),
                    "verification_output": "",
                    "files_changed": ops_result.get("files_changed", []),
                }
        step_result = coerce_execution_step_result(
            step_result,
            expected_files=expected_files,
            extract_structured_text=extract_structured_text,
        )
        if ops_result.get("files_changed"):
            merged_files_changed = list(
                dict.fromkeys(
                    list(ops_result.get("files_changed") or [])
                    + list(step_result.get("files_changed") or [])
                )
            )
            step_result["files_changed"] = merged_files_changed

        # Agent-initiated HITL: detect sentinel before assessment so the step
        # does not count as failed. The sentinel means "I need operator
        # confirmation before I proceed." We pause the session here; on resume
        # the operator's decision will be in context["human_guidance"] and the
        # agent retries the same step with that guidance.
        hitl_request = _parse_hitl_sentinel(step_result.get("output", ""))
        if hitl_request:
            intervention_type = hitl_request.get("intervention_type", "approval")
            if intervention_type not in {"guidance", "approval", "information"}:
                intervention_type = "approval"
            hitl_prompt = hitl_request.get("prompt") or (
                f"Agent requested {intervention_type} at step {step_index + 1}: "
                f"{step_description}"
            )
            hitl_context = dict(hitl_request.get("context") or {})
            hitl_context.update(
                {"step_index": step_index + 1, "step_description": step_description}
            )
            try:
                from app.services.session.intervention_service import (
                    create_intervention_request,
                )

                create_intervention_request(
                    db,
                    session_id=session_id,
                    project_id=project.id,
                    intervention_type=intervention_type,
                    prompt=hitl_prompt,
                    task_id=task_id,
                    context_snapshot=hitl_context,
                    initiated_by="ai",
                    revoke_running_tasks=False,  # we are the running task — stopping ourselves
                )
                emit_live(
                    "INFO",
                    f"[HITL] Agent requested {intervention_type} at step {step_index + 1}: "
                    f"{hitl_prompt[:200]}",
                    metadata={
                        "phase": "human_intervention",
                        "step_index": step_index + 1,
                        "intervention_type": intervention_type,
                    },
                )
            except Exception as _hitl_exc:
                logger.error(
                    "[HITL] Failed to create intervention request: %s", _hitl_exc
                )
            save_orchestration_checkpoint(
                db, session_id, task_id, prompt, orchestration_state
            )
            db.commit()
            return {
                "status": "awaiting_input",
                "task_id": task_id,
                "session_id": session_id,
                "step_index": step_index,
                "reason": "agent_requested_human_intervention",
            }

        step_debug_attempts = [
            da
            for da in orchestration_state.debug_attempts
            if da.get("attempt") is not None and da.get("step_index", -1) == step_index
        ]
        current_attempt = len(step_debug_attempts) + 1
        max_attempts = MAX_STEP_ATTEMPTS + (
            1 if orchestration_state.relaxed_mode else 0
        )

        assessment = assess_step_execution(
            db=db,
            session_id=session_id,
            task_id=task_id,
            project_dir=orchestration_state.project_dir,
            step=step,
            step_result=step_result,
            step_started_at=step_started_at,
            validation_profile=validation_profile,
            validation_severity=ctx.validation_severity,
            relaxed_mode=orchestration_state.relaxed_mode,
        )
        step_output = assessment.step_output
        step_status = assessment.step_status
        missing_files = assessment.missing_files
        stub_files = assessment.stub_files
        tool_failures = assessment.tool_failures
        correction_hints = assessment.correction_hints
        step_result["error"] = assessment.error_message

        # Audit-on-write: flag files created/modified outside the declared scope
        scope_violations = detect_scope_violations(
            orchestration_state.project_dir, expected_files, pre_step_checksum
        )
        if scope_violations:
            emit_live(
                "WARN",
                (
                    f"[WORKSPACE_GUARD] Step {step_index + 1} wrote "
                    f"{len(scope_violations)} file(s) outside declared scope: "
                    f"{', '.join(scope_violations[:6])}"
                ),
                metadata={
                    "phase": "executing",
                    "step_index": step_index + 1,
                    "scope_violations": scope_violations[:20],
                },
            )

        if missing_files:
            emit_live(
                "WARN",
                (
                    f"[ORCHESTRATION] Step {step_index + 1} reported success but "
                    f"did not materialize expected files: {', '.join(missing_files[:6])}"
                ),
                metadata={
                    "phase": "executing",
                    "step_index": step_index + 1,
                    "missing_expected_files": missing_files[:20],
                },
            )

        if stub_files:
            emit_live(
                "WARN",
                (
                    f"[ORCHESTRATION] Step {step_index + 1} created empty/stub files "
                    f"(exist on disk but have no content): {', '.join(stub_files[:6])}"
                ),
                metadata={
                    "phase": "executing",
                    "step_index": step_index + 1,
                    "stub_files": stub_files[:20],
                },
            )

        if tool_failures:
            emit_live(
                "WARN",
                (
                    f"[ORCHESTRATION] Step {step_index + 1} reported success but "
                    "task logs contain tool failures"
                ),
                metadata={
                    "phase": "executing",
                    "step_index": step_index + 1,
                    "tool_failures": tool_failures[:10],
                    "correction_hints": correction_hints[:10],
                },
            )

        if assessment.validation_verdict:
            record_validation_verdict(
                db,
                session_id,
                task_id,
                orchestration_state,
                assessment.validation_verdict,
                step_number=step_index + 1,
                parent_event_id=(step_started_event or {}).get("event_id"),
            )
            db.commit()
            if (
                not assessment.validation_verdict.accepted
                or assessment.validation_verdict.warning
            ):
                try:
                    maybe_emit_divergence_detected(
                        project_dir=orchestration_state.project_dir,
                        session_id=session_id,
                        task_id=task_id,
                        parent_event_id=(step_started_event or {}).get("event_id"),
                    )
                except Exception:
                    pass
            if assessment.validation_verdict.warning:
                emit_live(
                    "WARN",
                    f"[ORCHESTRATION] Step {step_index + 1} completed with validator warnings",
                    metadata={
                        "phase": "step_validation",
                        "step_index": step_index + 1,
                        "validation_status": assessment.validation_verdict.status,
                        "reasons": assessment.validation_verdict.reasons[:10],
                        "relaxed_mode": orchestration_state.relaxed_mode,
                    },
                )
            elif not assessment.validation_verdict.accepted:
                emit_live(
                    "WARN",
                    f"[ORCHESTRATION] Step {step_index + 1} failed validation after execution",
                    metadata={
                        "phase": "step_validation",
                        "step_index": step_index + 1,
                        "validation_status": assessment.validation_verdict.status,
                        "reasons": assessment.validation_verdict.reasons[:10],
                    },
                )

        step_record = StepResult(
            step_number=step_index + 1,
            status=step_status,
            output=step_output[:1000],
            verification_output=assessment.verification_output,
            files_changed=step_result.get("files_changed", expected_files),
            error_message=step_result.get("error", ""),
            attempt=current_attempt,
        )
        runtime_metadata = (
            runtime_service.get_backend_metadata()
            if runtime_service and hasattr(runtime_service, "get_backend_metadata")
            else {}
        )
        failure_envelope = FailureEnvelope(
            session_id=session_id,
            task_id=task_id,
            phase="debugging",
            step_index=step_index + 1,
            model_id=":".join(
                part
                for part in [
                    str(runtime_metadata.get("backend") or "").strip(),
                    str(runtime_metadata.get("model_family") or "").strip(),
                ]
                if part
            ),
            input={
                "step_description": step_description,
                "commands": list(step_commands or []),
                "verification": verification_command,
                "expected_files": list(expected_files or []),
            },
            output={
                "step_status": step_status,
                "files_changed": list(step_record.files_changed or []),
                "tool_failures": list(assessment.tool_failures or [])[:10],
            },
            stderr="\n".join(
                part
                for part in [
                    step_record.error_message,
                    step_record.verification_output,
                ]
                if part
            )[:1200],
            root_cause=classify_failure_root_cause(
                error_message=step_record.error_message,
                verification_output=step_record.verification_output,
                tool_failures=assessment.tool_failures,
            ),
        )
        debug_feedback_envelope = None
        if step_status == "failed":
            debug_feedback_envelope = build_debug_feedback_envelope(
                task_execution_id=ctx.task_execution_id,
                task_id=task_id,
                step_index=step_index + 1,
                failure_phase=(
                    "step_validation"
                    if assessment.validation_verdict
                    and not assessment.validation_verdict.accepted
                    else "execution"
                ),
                failed_command=(
                    str(verification_command or "").strip()
                    or " && ".join(str(command) for command in (step_commands or []))
                ),
                return_code=step_result.get("returncode"),
                stdout=step_output,
                stderr="\n".join(
                    part
                    for part in [
                        step_record.error_message,
                        step_record.verification_output,
                    ]
                    if part
                ),
                validator_reasons=(
                    assessment.validation_verdict.reasons
                    if assessment.validation_verdict
                    else []
                ),
                changed_files=step_record.files_changed,
                workspace_path=orchestration_state.project_dir,
            )
        step_finished_event = None
        try:
            step_finished_event = append_orchestration_event(
                project_dir=orchestration_state.project_dir,
                session_id=session_id,
                task_id=task_id,
                event_type=EventType.STEP_FINISHED,
                parent_event_id=(step_started_event or {}).get("event_id"),
                details={
                    "step_index": step_index + 1,
                    "step_total": len(orchestration_state.plan),
                    "status": step_status,
                    "error": step_record.error_message[:240],
                },
            )
        except Exception:
            pass

        if step_status == "success":
            # Chain-of-Verification: confirm and surface the actual workspace changes
            cove_changes = summarize_step_changes(
                pre_step_checksum, orchestration_state.project_dir
            )
            if cove_changes:
                emit_live(
                    "INFO",
                    (
                        f"[WORKSPACE_GUARD] CoVe: step {step_index + 1} verified "
                        f"{len(cove_changes)} change(s): {', '.join(cove_changes[:8])}"
                    ),
                    metadata={
                        "phase": "executing",
                        "step_index": step_index + 1,
                        "cove_changes": cove_changes[:20],
                    },
                )
            orchestration_state.record_success(step_record)
            tool_events = [
                event
                for event in (
                    read_orchestration_events(
                        orchestration_state.project_dir, session_id, task_id
                    )
                )[-20:]
                if event.get("event_type") == EventType.TOOL_INVOKED
            ]
            try:
                emit_intent_outcome_mismatch(
                    project_dir=orchestration_state.project_dir,
                    session_id=session_id,
                    task_id=task_id,
                    step_index=step_index + 1,
                    step_description=step_description,
                    expected_files=expected_files,
                    actual_files=step_record.files_changed,
                    actual_tool_calls=[
                        str((event.get("details") or {}).get("tool_name") or "")
                        for event in tool_events
                    ],
                    parent_event_id=(step_finished_event or {}).get("event_id"),
                )
            except Exception:
                pass
            save_orchestration_checkpoint(
                db, session_id, task_id, prompt, orchestration_state
            )
            logger.info(
                "[ORCHESTRATION] Step %s completed successfully", step_index + 1
            )
            emit_live(
                "INFO",
                f"[ORCHESTRATION] Step {step_index + 1} completed successfully",
                metadata={"phase": "executing", "step_index": step_index + 1},
            )
            continue

        extra_context = ""
        if step_status == "failed":
            cleanup_summary = ExecutorService.cleanup_failed_step_artefacts(
                project_dir=orchestration_state.project_dir,
                step=step,
                logger=logger,
                emit_live=emit_live,
            )

            # Surface the cleanup info in the debug prompt so the model knows
            # which files it must re-generate from scratch.
            if cleanup_summary["removed_files"]:
                extra_context = (
                    "\\n\\nNote: the following files were empty/stub after the failed step "
                    "and have been removed so you must regenerate their full content: "
                    + ", ".join(cleanup_summary["removed_files"][:10])
                )

            if scope_violations:
                extra_context += (
                    "\\n\\nNote: these files were written outside the step's declared "
                    "expected_files scope (unexpected side effects to be aware of): "
                    + ", ".join(scope_violations[:10])
                )

        orchestration_state.record_failure(step_record)
        save_orchestration_checkpoint(
            db, session_id, task_id, prompt, orchestration_state
        )

        logger.info(
            "[ORCHESTRATION] Step %s failed, entering DEBUGGING phase",
            step_index + 1,
        )
        emit_live(
            "WARN",
            f"[ORCHESTRATION] Step {step_index + 1} failed, entering DEBUGGING phase",
            metadata={"phase": "debugging", "step_index": step_index + 1},
        )
        debugging_phase_event = None
        try:
            debugging_phase_event = append_orchestration_event(
                project_dir=orchestration_state.project_dir,
                session_id=session_id,
                task_id=task_id,
                event_type=EventType.PHASE_STARTED,
                parent_event_id=(step_finished_event or step_started_event or {}).get(
                    "event_id"
                ),
                details={
                    "phase": "debugging",
                    "step_index": step_index + 1,
                    "attempt": current_attempt,
                },
            )
        except Exception:
            pass
        try:
            retry_event = append_orchestration_event(
                project_dir=orchestration_state.project_dir,
                session_id=session_id,
                task_id=task_id,
                event_type=EventType.RETRY_ENTERED,
                parent_event_id=(step_finished_event or step_started_event or {}).get(
                    "event_id"
                ),
                details=attach_failure_envelope(
                    {
                        "step_index": step_index + 1,
                        "attempt": current_attempt,
                        "reason": step_record.error_message[:240],
                    },
                    failure_envelope,
                ),
            )
            write_orchestration_state_snapshot(
                project_dir=orchestration_state.project_dir,
                session_id=session_id,
                task_id=task_id,
                orchestration_state=orchestration_state,
                trigger="retry_entered",
                related_event_id=retry_event.get("event_id"),
            )
            maybe_emit_divergence_detected(
                project_dir=orchestration_state.project_dir,
                session_id=session_id,
                task_id=task_id,
                parent_event_id=retry_event.get("event_id"),
            )
        except Exception:
            pass

        if ExecutorService.is_repeated_tool_path_failure(
            orchestration_state.debug_attempts, step_record.error_message
        ):
            decision = repeated_tool_path_failure_decision(
                step_index=step_index,
                execution_profile=execution_profile,
                validation_profile=validation_profile,
                expected_files=expected_files,
                step=step,
                project_dir=orchestration_state.project_dir,
                error_message=step_record.error_message,
                relaxed_mode=orchestration_state.relaxed_mode,
            )
            if decision.action == "rewrite_step":
                rewritten_step = decision.rewritten_step or step
                orchestration_state.plan[step_index] = rewritten_step
                task.steps = json.dumps(orchestration_state.plan)
                orchestration_state.debug_attempts.append(
                    {
                        "attempt": len(orchestration_state.debug_attempts) + 1,
                        "fix_type": "command_fix",
                        "fix": "Rewrote inspection step into workspace discovery commands after repeated guessed-path failures",
                        "analysis": step_record.error_message[:500],
                        "confidence": "HIGH",
                        "error": step_record.error_message,
                    }
                )
                save_orchestration_checkpoint(
                    db, session_id, task_id, prompt, orchestration_state
                )
                db.commit()
                emit_live(
                    "WARN",
                    f"[ORCHESTRATION] {decision.message}",
                    metadata={
                        "phase": "debugging",
                        "step_index": step_index + 1,
                        "reason": "repeated_tool_path_failure_rewritten_step",
                        "relaxed_mode": orchestration_state.relaxed_mode,
                    },
                )
                continue

            manual_gate_message = decision.message
            logger.warning("[ORCHESTRATION] %s", manual_gate_message)
            emit_live(
                "ERROR",
                f"[ORCHESTRATION] {manual_gate_message}",
                metadata={
                    "phase": "debugging",
                    "step_index": step_index + 1,
                    "manual_review_required": True,
                    "reason": "repeated_tool_path_failure",
                },
            )
            orchestration_state.status = OrchestrationStatus.ABORTED
            orchestration_state.abort_reason = manual_gate_message
            mark_task_attempt_failed(
                task=task,
                session_task_link=session_task_link,
                task_execution=_get_task_execution(db, ctx.task_execution_id),
                error_message=manual_gate_message,
                completed_at=datetime.now(timezone.utc),
            )
            db.commit()
            try:
                phase_finished_event = append_orchestration_event(
                    project_dir=orchestration_state.project_dir,
                    session_id=session_id,
                    task_id=task_id,
                    event_type=EventType.PHASE_FINISHED,
                    parent_event_id=(debugging_phase_event or {}).get("event_id"),
                    details={
                        "phase": "debugging",
                        "status": "manual_review_required",
                        "step_index": step_index + 1,
                    },
                )
                write_orchestration_state_snapshot(
                    project_dir=orchestration_state.project_dir,
                    session_id=session_id,
                    task_id=task_id,
                    orchestration_state=orchestration_state,
                    trigger="phase_finished",
                    related_event_id=phase_finished_event.get("event_id"),
                )
            except Exception:
                pass
            mark_session_paused(
                session,
                alert_level="error",
                alert_message=manual_gate_message,
            )
            restore_workspace_snapshot_if_needed("repeated tool/path failures")
            write_project_state_snapshot_fn(db, project, task, session_id)
            return {"status": "failed", "reason": "manual_review_required"}

        if (
            step_status == "failed"
            and current_attempt == 1
            and tool_failures
            and ExecutorService.should_short_circuit_to_workspace_discovery(
                tool_failures, orchestration_state.project_dir
            )
        ):
            decision = repeated_tool_path_failure_decision(
                step_index=step_index,
                execution_profile=execution_profile,
                validation_profile=validation_profile,
                expected_files=expected_files,
                step=step,
                project_dir=orchestration_state.project_dir,
                error_message=step_record.error_message,
                relaxed_mode=orchestration_state.relaxed_mode,
            )
            if decision.action == "rewrite_step":
                rewritten_step = decision.rewritten_step or step
                orchestration_state.plan[step_index] = rewritten_step
                task.steps = json.dumps(orchestration_state.plan)
                orchestration_state.debug_attempts.append(
                    {
                        "attempt": len(orchestration_state.debug_attempts) + 1,
                        "fix_type": "command_fix",
                        "fix": "Rewrote inspection step into workspace discovery commands after directory-read tool failure",
                        "analysis": step_record.error_message[:500],
                        "confidence": "HIGH",
                        "error": step_record.error_message,
                    }
                )
                save_orchestration_checkpoint(
                    db, session_id, task_id, prompt, orchestration_state
                )
                db.commit()
                emit_live(
                    "WARN",
                    (
                        f"[ORCHESTRATION] Step {step_index + 1} hit a directory-read "
                        "tool failure and was rewritten into a workspace-discovery step"
                    ),
                    metadata={
                        "phase": "debugging",
                        "step_index": step_index + 1,
                        "reason": "directory_read_failure_rewritten_step",
                    },
                )
                continue

        if (
            current_attempt >= max_attempts
            and not orchestration_state.relaxed_mode
            and ctx.policy_profile.allow_relaxed_mode
        ):
            orchestration_state.relaxed_mode = True
            orchestration_state.debug_attempts.append(
                {
                    "attempt": len(orchestration_state.debug_attempts) + 1,
                    "fix_type": "relaxed_mode",
                    "fix": "Enabled relaxed orchestration mode after repeated step failures",
                    "analysis": step_record.error_message[:500],
                    "confidence": "MEDIUM",
                    "error": step_record.error_message,
                    "step_index": step_index,
                }
            )
            save_orchestration_checkpoint(
                db, session_id, task_id, prompt, orchestration_state
            )
            db.commit()
            emit_live(
                "WARN",
                (
                    f"[ORCHESTRATION] Step {step_index + 1} hit the normal retry limit; "
                    "switching to relaxed mode for one more repair attempt"
                ),
                metadata={
                    "phase": "debugging",
                    "step_index": step_index + 1,
                    "relaxed_mode": True,
                },
            )
            max_attempts = MAX_STEP_ATTEMPTS + 1

        if current_attempt >= max_attempts:
            emit_live(
                "ERROR",
                f"[ORCHESTRATION] Step {step_index + 1} failed after {current_attempt} attempts, marking as failed",
                metadata={
                    "phase": "debugging",
                    "step_index": step_index + 1,
                    "max_attempts_reached": True,
                },
            )
            orchestration_state.status = OrchestrationStatus.ABORTED
            orchestration_state.abort_reason = (
                f"Step {step_index + 1} failed after {current_attempt} attempts"
            )
            error_message = (
                f"Step failed after {current_attempt} attempts: "
                f"{step_record.error_message[:500]}"
            )
            mark_task_attempt_failed(
                task=task,
                session_task_link=session_task_link,
                task_execution=_get_task_execution(db, ctx.task_execution_id),
                error_message=error_message,
                completed_at=datetime.now(timezone.utc),
            )
            mark_session_paused(
                session,
                alert_level="error",
                alert_message=error_message[:2000],
            )
            db.commit()
            try:
                phase_finished_event = append_orchestration_event(
                    project_dir=orchestration_state.project_dir,
                    session_id=session_id,
                    task_id=task_id,
                    event_type=EventType.PHASE_FINISHED,
                    parent_event_id=(debugging_phase_event or {}).get("event_id"),
                    details={
                        "phase": "debugging",
                        "status": "max_attempts_reached",
                        "step_index": step_index + 1,
                    },
                )
                write_orchestration_state_snapshot(
                    project_dir=orchestration_state.project_dir,
                    session_id=session_id,
                    task_id=task_id,
                    orchestration_state=orchestration_state,
                    trigger="phase_finished",
                    related_event_id=phase_finished_event.get("event_id"),
                )
            except Exception:
                pass
            restore_workspace_snapshot_if_needed("max step attempts reached")
            write_project_state_snapshot_fn(db, project, task, session_id)
            return {"status": "failed", "reason": "max_attempts_reached"}

        debug_inputs = DebugPromptInputs(
            step_description=step_description + extra_context,
            error_message=step_record.error_message,
            command_output=step_output,
            verification_output=step_record.verification_output,
            attempt_number=current_attempt,
            max_attempts=max_attempts,
            failure_envelope=failure_envelope,
        )
        task_execution_id = ctx.task_execution_id
        debug_repair_used_ids = set(
            int(item)
            for item in (
                getattr(orchestration_state, "debug_repair_task_execution_ids", [])
                or []
            )
            if str(item).isdigit()
        )
        phase7f_debug_repair_allowed = (
            debug_feedback_envelope is not None
            and debug_feedback_envelope.eligible_for_debug_repair
            and task_execution_id is not None
            and int(task_execution_id) not in debug_repair_used_ids
        )
        if (
            debug_feedback_envelope is not None
            and debug_feedback_envelope.eligible_for_debug_repair
            and task_execution_id is not None
            and int(task_execution_id) in debug_repair_used_ids
        ):
            terminal_message = (
                "Phase 7F debug repair budget exhausted for this TaskExecution"
            )
            emit_live(
                "ERROR",
                f"[ORCHESTRATION] {terminal_message}",
                metadata={
                    "phase": "debugging",
                    "step_index": step_index + 1,
                    "debug_repair_terminal_reason": "debug_repair_budget_exhausted",
                    "debug_failure_class": debug_feedback_envelope.failure_class,
                    "task_execution_id": task_execution_id,
                },
            )
            try:
                append_orchestration_event(
                    project_dir=orchestration_state.project_dir,
                    session_id=session_id,
                    task_id=task_id,
                    event_type=EventType.REPAIR_REJECTED,
                    parent_event_id=(debugging_phase_event or {}).get("event_id"),
                    details={
                        "phase": "execution",
                        "reason": "debug_repair_budget_exhausted",
                        "debug_repair_terminal_reason": (
                            "debug_repair_budget_exhausted"
                        ),
                        "debug_repair_attempted": False,
                        "debug_repair_used": True,
                        "debug_failure_class": debug_feedback_envelope.failure_class,
                        "task_execution_id": task_execution_id,
                        "step_index": step_index + 1,
                    },
                )
            except Exception:
                pass
            orchestration_state.status = OrchestrationStatus.ABORTED
            orchestration_state.abort_reason = terminal_message
            mark_task_attempt_failed(
                task=task,
                session_task_link=session_task_link,
                task_execution=_get_task_execution(db, task_execution_id),
                error_message=terminal_message + f": {step_record.error_message[:500]}",
                completed_at=datetime.now(timezone.utc),
            )
            db.commit()
            restore_workspace_snapshot_if_needed("debug repair budget exhausted")
            write_project_state_snapshot_fn(db, project, task, session_id)
            return {"status": "failed", "reason": "debug_repair_budget_exhausted"}

        if phase7f_debug_repair_allowed:
            _evidence_capsule = collect_workspace_evidence(
                debug_feedback_envelope.failure_class,
                orchestration_state.project_dir,
                failure_context=debug_feedback_envelope.stderr_excerpt,
            )
            persist_debug_feedback_envelope(
                db=db,
                session_id=session_id,
                task_id=task_id,
                session_instance_id=session.instance_id if session else None,
                project_dir=orchestration_state.project_dir,
                envelope=debug_feedback_envelope,
                parent_event_id=(step_finished_event or step_started_event or {}).get(
                    "event_id"
                ),
                evidence_capsule=_evidence_capsule,
            )
            if _evidence_capsule and not _evidence_capsule.is_empty():
                append_orchestration_event(
                    project_dir=orchestration_state.project_dir,
                    session_id=session_id,
                    task_id=task_id,
                    event_type=EventType.WORKSPACE_EVIDENCE_COLLECTED,
                    parent_event_id=(
                        step_finished_event or step_started_event or {}
                    ).get("event_id"),
                    details={
                        "phase": "execution",
                        "failure_class": debug_feedback_envelope.failure_class,
                        "evidence_chars_total": _evidence_capsule.total_chars,
                        "evidence_files_inspected": _evidence_capsule.files_inspected,
                        "evidence_matched_lines": _evidence_capsule.matched_line_count,
                        "commands_run": _evidence_capsule.commands_run,
                    },
                )
            diff_capsule = build_diff_capsule(
                pre_checksum=pre_step_file_snapshot,
                project_dir=orchestration_state.project_dir,
                changed_files=debug_feedback_envelope.changed_files,
                envelope=debug_feedback_envelope,
            )
            if diff_capsule is not None:
                debug_prompt = build_bounded_diff_repair_prompt(
                    diff_capsule, _evidence_capsule
                )
                debug_prompt_mode = "phase7g_diff_repair"
                diff_repair_fallback_reason = None
            else:
                debug_prompt = build_bounded_debug_repair_prompt(
                    debug_feedback_envelope, _evidence_capsule
                )
                debug_prompt_mode = "phase7f_bounded_debug_repair"
                if not debug_feedback_envelope.changed_files:
                    diff_repair_fallback_reason = "no_changed_files"
                elif debug_feedback_envelope.failure_class not in {
                    "pytest_failure",
                    "runtime_assertion_failure",
                    "syntax_error",
                    "import_error",
                }:
                    diff_repair_fallback_reason = "ineligible_failure_class"
                else:
                    diff_repair_fallback_reason = "diff_capsule_unavailable"
            orchestration_state.debug_repair_task_execution_ids = sorted(
                {*debug_repair_used_ids, int(task_execution_id)}
            )
            save_orchestration_checkpoint(
                db, session_id, task_id, prompt, orchestration_state
            )
            db.commit()
        else:
            diff_capsule = None
            diff_repair_fallback_reason = None
            debug_prompt = assemble_debugging_prompt(ctx, debug_inputs)
            debug_prompt_mode = "legacy_debugging"
            if debug_feedback_envelope is not None:
                persist_debug_feedback_envelope(
                    db=db,
                    session_id=session_id,
                    task_id=task_id,
                    session_instance_id=session.instance_id if session else None,
                    project_dir=orchestration_state.project_dir,
                    envelope=debug_feedback_envelope,
                    parent_event_id=(
                        step_finished_event or step_started_event or {}
                    ).get("event_id"),
                )
                db.commit()

        try:
            append_orchestration_event(
                project_dir=orchestration_state.project_dir,
                session_id=session_id,
                task_id=task_id,
                event_type=EventType.DEBUG_REPAIR_ATTEMPTED,
                parent_event_id=(debugging_phase_event or {}).get("event_id"),
                details={
                    "phase": "execution",
                    "debug_repair_attempted": True,
                    "debug_repair_used": True,
                    "debug_failure_class": (
                        debug_feedback_envelope.failure_class
                        if debug_feedback_envelope
                        else failure_envelope.root_cause
                    ),
                    "debug_repair_step_count": 1,
                    "task_execution_id": ctx.task_execution_id,
                    "step_index": step_index + 1,
                    "attempt": current_attempt,
                    "allowed": phase7f_debug_repair_allowed,
                    "allowed_reason": (
                        "eligible failure class and no prior debug repair for TaskExecution"
                        if phase7f_debug_repair_allowed
                        else "Phase 7F bounded repair not eligible or already used for TaskExecution"
                    ),
                    "debug_prompt_mode": debug_prompt_mode,
                    "envelope_mode": (
                        "direct_capsule"
                        if debug_prompt_mode
                        in {
                            "phase7g_diff_repair",
                            "phase7f_bounded_debug_repair",
                        }
                        else "legacy_envelope"
                    ),
                    "compliance_retry_attempted": False,
                    "compliance_retry_succeeded": False,
                    "diff_capsule_primary_file": (
                        diff_capsule.primary_file if diff_capsule else None
                    ),
                    "diff_capsule_line_count": (
                        diff_capsule.diff_line_count if diff_capsule else 0
                    ),
                    "diff_repair_fallback_reason": diff_repair_fallback_reason,
                },
            )
        except Exception:
            pass

        debug_result = _run_coroutine(
            runtime_service.execute_task(
                debug_prompt, timeout_seconds=DEBUG_TIMEOUT_SECONDS
            )
        )

        if debug_result.get("error") == "Context window exceeded":
            logger.warning(
                "[ORCHESTRATION] Debug prompt exceeded context window at step %s; "
                "retrying with compact prompt",
                step_index + 1,
            )
            emit_live(
                "WARN",
                f"[ORCHESTRATION] Debug prompt exceeded context window; retrying compact for step {step_index + 1}",
                metadata={
                    "phase": "debugging",
                    "step_index": step_index + 1,
                    "compact_retry": True,
                },
            )
            compact_debug_prompt = assemble_debugging_prompt(
                ctx,
                DebugPromptInputs(
                    step_description=step_description,
                    error_message=step_record.error_message,
                    command_output=step_output,
                    verification_output=step_record.verification_output,
                    attempt_number=current_attempt,
                    max_attempts=max_attempts,
                    compact=True,
                    failure_envelope=failure_envelope,
                ),
            )
            debug_result = _run_coroutine(
                runtime_service.execute_task(
                    compact_debug_prompt, timeout_seconds=DEBUG_TIMEOUT_SECONDS
                )
            )

        # Remove debug artifacts the agent may have written to disk.
        for _artifact in ("debug_report.json", "analysis.json", "debug_analysis.json"):
            _artifact_path = orchestration_state.project_dir / _artifact
            if _artifact_path.exists() and _artifact_path.is_file():
                _artifact_path.unlink(missing_ok=True)
                logger.info("[ORCHESTRATION] Removed debug artifact: %s", _artifact)

        try:
            if phase7f_debug_repair_allowed:
                repair_output = extract_structured_text(
                    debug_result.get("output", "{}")
                )
                success, parsed_repair, strategy_info = (
                    error_handler.attempt_json_parsing(
                        repair_output, context="phase7f_debug_repair"
                    )
                )
                compliance_retry_attempted = False
                compliance_retry_succeeded = False
                if not success:
                    compliance_retry_attempted = True
                    compliance_prompt = build_json_compliance_retry_prompt(
                        repair_output,
                        expected_shape="array or object",
                    )
                    try:
                        compliance_result = _run_coroutine(
                            runtime_service.execute_task(
                                compliance_prompt,
                                timeout_seconds=DEBUG_TIMEOUT_SECONDS,
                            )
                        )
                        compliance_output = extract_structured_text(
                            compliance_result.get("output", "{}")
                        )
                        success, parsed_repair, strategy_info = (
                            error_handler.attempt_json_parsing(
                                compliance_output,
                                context="phase7f_debug_repair_compliance_retry",
                            )
                        )
                    except Exception as compliance_error:
                        success = False
                        parsed_repair = None
                        strategy_info = (
                            "Compliance retry failed: " f"{str(compliance_error)[:200]}"
                        )
                    compliance_retry_succeeded = bool(success)
                    try:
                        append_orchestration_event(
                            project_dir=orchestration_state.project_dir,
                            session_id=session_id,
                            task_id=task_id,
                            event_type=EventType.DEBUG_REPAIR_ATTEMPTED,
                            parent_event_id=(debugging_phase_event or {}).get(
                                "event_id"
                            ),
                            details={
                                "phase": "execution",
                                "debug_repair_attempted": True,
                                "debug_repair_used": True,
                                "debug_prompt_mode": debug_prompt_mode,
                                "envelope_mode": "direct_capsule",
                                "task_execution_id": ctx.task_execution_id,
                                "step_index": step_index + 1,
                                "compliance_retry_attempted": (
                                    compliance_retry_attempted
                                ),
                                "compliance_retry_succeeded": (
                                    compliance_retry_succeeded
                                ),
                            },
                        )
                    except Exception:
                        pass
                debug_data = (
                    normalize_bounded_debug_repair_payload(parsed_repair)
                    if success
                    else None
                )
                if not success or debug_data is None:
                    append_orchestration_event(
                        project_dir=orchestration_state.project_dir,
                        session_id=session_id,
                        task_id=task_id,
                        event_type=EventType.REPAIR_REJECTED,
                        parent_event_id=(debugging_phase_event or {}).get("event_id"),
                        details={
                            "phase": "execution",
                            "reason": "phase7f_debug_repair_output_invalid",
                            "debug_repair_terminal_reason": (
                                "invalid_debug_repair_output"
                            ),
                            "debug_repair_attempted": True,
                            "debug_repair_used": True,
                            "debug_failure_class": (
                                debug_feedback_envelope.failure_class
                                if debug_feedback_envelope
                                else None
                            ),
                            "task_execution_id": ctx.task_execution_id,
                            "strategy": strategy_info,
                            "compliance_retry_attempted": (compliance_retry_attempted),
                            "compliance_retry_succeeded": (compliance_retry_succeeded),
                        },
                    )
                    raise ValueError(
                        f"Invalid Phase 7F debug repair output: {strategy_info}"
                    )
            else:
                success, debug_data, strategy_info = coerce_debug_step_result(
                    debug_result,
                    error_message=step_record.error_message,
                    step=step,
                    extract_structured_text=extract_structured_text,
                )
                if not success:
                    raise ValueError(f"Failed to parse debug result: {strategy_info}")

            fix_type = debug_data.get("fix_type", "code_fix")
            logger.info("[DEBUG-PARSE] Using strategy: %s", strategy_info)

            max_plan_revisions = ctx.policy_profile.max_plan_revisions
            if fix_type == "revise_plan" and plan_revision_count >= max_plan_revisions:
                logger.warning(
                    "[ORCHESTRATION] Plan revision cap (%d) reached; aborting instead of re-planning",
                    max_plan_revisions,
                )
                emit_live(
                    "ERROR",
                    f"[ORCHESTRATION] Plan revision cap ({max_plan_revisions}) reached; aborting",
                    metadata={"phase": "plan_revision", "step_index": step_index + 1},
                )
                orchestration_state.status = OrchestrationStatus.ABORTED
                orchestration_state.abort_reason = f"Plan revision cap ({max_plan_revisions}) reached after step {step_index + 1}"
                mark_task_attempt_failed(
                    task=task,
                    session_task_link=session_task_link,
                    task_execution=_get_task_execution(db, task_execution_id),
                    error_message=orchestration_state.abort_reason,
                    completed_at=datetime.now(timezone.utc),
                )
                db.commit()
                restore_workspace_snapshot_if_needed("max step attempts reached")
                write_project_state_snapshot_fn(db, project, task, session_id)
                return {"status": "failed", "reason": "plan_revision_cap_reached"}

            if fix_type == "revise_plan":
                plan_revision_count += 1
                logger.info(
                    "[ORCHESTRATION] Plan revision needed, entering PLAN_REVISION phase"
                )
                emit_live(
                    "WARN",
                    "[ORCHESTRATION] Plan revision needed, entering PLAN_REVISION phase",
                    metadata={"phase": "plan_revision", "step_index": step_index + 1},
                )
                try:
                    append_orchestration_event(
                        project_dir=orchestration_state.project_dir,
                        session_id=session_id,
                        task_id=task_id,
                        event_type=EventType.PHASE_FINISHED,
                        parent_event_id=(debugging_phase_event or {}).get("event_id"),
                        details={
                            "phase": "debugging",
                            "status": "handed_off_to_plan_revision",
                            "step_index": step_index + 1,
                        },
                    )
                except Exception:
                    pass
                plan_revision_phase_event = None
                try:
                    plan_revision_phase_event = append_orchestration_event(
                        project_dir=orchestration_state.project_dir,
                        session_id=session_id,
                        task_id=task_id,
                        event_type=EventType.PHASE_STARTED,
                        parent_event_id=(debugging_phase_event or {}).get("event_id"),
                        details={
                            "phase": "plan_revision",
                            "step_index": step_index + 1,
                        },
                    )
                    write_orchestration_state_snapshot(
                        project_dir=orchestration_state.project_dir,
                        session_id=session_id,
                        task_id=task_id,
                        orchestration_state=orchestration_state,
                        trigger="phase_started",
                        related_event_id=plan_revision_phase_event.get("event_id"),
                    )
                except Exception:
                    pass
                revise_prompt = assemble_plan_revision_prompt(
                    ctx,
                    failed_steps=[step_record],
                    debug_analysis=debug_result.get("output", ""),
                )
                revise_result = _run_coroutine(
                    runtime_service.execute_task(
                        revise_prompt, timeout_seconds=DEBUG_TIMEOUT_SECONDS
                    )
                )
                revise_output = revise_result.get("output", "{}")
                success, revise_data, strategy_info = (
                    error_handler.attempt_json_parsing(
                        revise_output, context="revision"
                    )
                )
                if not success:
                    raise ValueError(f"Failed to parse revision: {strategy_info}")

                orchestration_state.plan = normalize_plan_with_live_logging(
                    db,
                    session_id,
                    task_id,
                    revise_data.get("revised_plan", orchestration_state.plan),
                    orchestration_state.project_dir,
                    logger,
                    session.instance_id,
                    "Plan revision",
                )
                revised_plan_verdict = ValidatorService.validate_plan(
                    orchestration_state.plan,
                    output_text=revise_output,
                    task_prompt=prompt,
                    execution_profile=execution_profile,
                    project_dir=orchestration_state.project_dir,
                    title=task.title if task else None,
                    description=task.description if task else None,
                    validation_severity=ctx.validation_severity,
                    workflow_profile=getattr(ctx, "workflow_profile", None),
                )
                record_validation_verdict(
                    db,
                    session_id,
                    task_id,
                    orchestration_state,
                    revised_plan_verdict.verdict,
                    parent_event_id=(plan_revision_phase_event or {}).get("event_id"),
                )
                db.commit()
                if not revised_plan_verdict.accepted:
                    revised_plan_error = "Revised plan failed validation: " + "; ".join(
                        revised_plan_verdict.reasons[:3]
                    )
                    orchestration_state.status = OrchestrationStatus.ABORTED
                    orchestration_state.abort_reason = revised_plan_error
                    mark_task_attempt_failed(
                        task=task,
                        session_task_link=session_task_link,
                        task_execution=_get_task_execution(db, task_execution_id),
                        error_message=revised_plan_error,
                        completed_at=datetime.now(timezone.utc),
                    )
                    emit_live(
                        "ERROR",
                        "[ORCHESTRATION] Revised plan failed validation",
                        metadata={
                            "phase": "plan_revision",
                            "validation_status": revised_plan_verdict.status,
                            "reasons": revised_plan_verdict.reasons[:10],
                        },
                    )
                    db.commit()
                    try:
                        phase_finished_event = append_orchestration_event(
                            project_dir=orchestration_state.project_dir,
                            session_id=session_id,
                            task_id=task_id,
                            event_type=EventType.PHASE_FINISHED,
                            parent_event_id=(plan_revision_phase_event or {}).get(
                                "event_id"
                            ),
                            details={
                                "phase": "plan_revision",
                                "status": "revised_plan_validation_failed",
                                "step_index": step_index + 1,
                            },
                        )
                        write_orchestration_state_snapshot(
                            project_dir=orchestration_state.project_dir,
                            session_id=session_id,
                            task_id=task_id,
                            orchestration_state=orchestration_state,
                            trigger="phase_finished",
                            related_event_id=phase_finished_event.get("event_id"),
                        )
                    except Exception:
                        pass
                    restore_workspace_snapshot_if_needed(
                        "revised plan validation failure"
                    )
                    write_project_state_snapshot_fn(db, project, task, session_id)
                    return {
                        "status": "failed",
                        "reason": "revised_plan_validation_failed",
                    }

                logger.info(
                    "[ORCHESTRATION] Plan revised, %s steps",
                    len(orchestration_state.plan),
                )
                emit_live(
                    "INFO",
                    f"[ORCHESTRATION] Plan revised, {len(orchestration_state.plan)} steps",
                    metadata={
                        "phase": "plan_revision",
                        "steps": len(orchestration_state.plan),
                        "strategy": strategy_info,
                    },
                )
                try:
                    append_orchestration_event(
                        project_dir=orchestration_state.project_dir,
                        session_id=session_id,
                        task_id=task_id,
                        event_type=EventType.PLAN_REVISED,
                        parent_event_id=(plan_revision_phase_event or {}).get(
                            "event_id"
                        ),
                        details={
                            "step_index": step_index + 1,
                            "steps": len(orchestration_state.plan),
                            "strategy": strategy_info,
                        },
                    )
                except Exception:
                    pass
                try:
                    phase_finished_event = append_orchestration_event(
                        project_dir=orchestration_state.project_dir,
                        session_id=session_id,
                        task_id=task_id,
                        event_type=EventType.PHASE_FINISHED,
                        parent_event_id=(plan_revision_phase_event or {}).get(
                            "event_id"
                        ),
                        details={
                            "phase": "plan_revision",
                            "status": "completed",
                            "step_index": step_index + 1,
                        },
                    )
                    write_orchestration_state_snapshot(
                        project_dir=orchestration_state.project_dir,
                        session_id=session_id,
                        task_id=task_id,
                        orchestration_state=orchestration_state,
                        trigger="phase_finished",
                        related_event_id=phase_finished_event.get("event_id"),
                    )
                except Exception:
                    pass
                continue

            if fix_type in {"code_fix", "command_fix", "ops_fix"}:
                debug_files_changed = debug_result.get("files_changed")
                debug_changed_files = (
                    debug_files_changed if isinstance(debug_files_changed, list) else []
                )
                structured_ops_present = isinstance(step.get("ops"), list) and bool(
                    step.get("ops")
                )
                actionable_step_fields = any(
                    key in debug_data for key in ("expected_files", "verification")
                )
                if (
                    fix_type == "code_fix"
                    and structured_ops_present
                    and not debug_changed_files
                    and not actionable_step_fields
                ):
                    reason = "non_actionable_code_fix_for_structured_ops"
                    logger.warning(
                        "[ORCHESTRATION] Rejecting non-actionable code_fix before retrying structured ops for step %s",
                        step_index + 1,
                    )
                    emit_live(
                        "ERROR",
                        "[ORCHESTRATION] Debug repair was not actionable for structured file operations; stopping instead of retrying the same failed ops",
                        metadata={
                            "phase": "debugging",
                            "step_index": step_index + 1,
                            "reason": reason,
                            "fix_type": fix_type,
                            "structured_ops_present": True,
                        },
                    )
                    orchestration_state.status = OrchestrationStatus.ABORTED
                    orchestration_state.abort_reason = (
                        "Debug repair was not actionable for structured file operations"
                    )
                    mark_task_attempt_failed(
                        task=task,
                        session_task_link=session_task_link,
                        task_execution=_get_task_execution(db, task_execution_id),
                        error_message=orchestration_state.abort_reason,
                        completed_at=datetime.now(timezone.utc),
                    )
                    db.commit()
                    try:
                        phase_finished_event = append_orchestration_event(
                            project_dir=orchestration_state.project_dir,
                            session_id=session_id,
                            task_id=task_id,
                            event_type=EventType.REPAIR_REJECTED,
                            parent_event_id=(debugging_phase_event or {}).get(
                                "event_id"
                            ),
                            details={
                                "phase": "debugging",
                                "status": "repair_rejected",
                                "step_index": step_index + 1,
                                "reason": reason,
                                "fix_type": fix_type,
                                "structured_ops_present": True,
                            },
                        )
                        write_orchestration_state_snapshot(
                            project_dir=orchestration_state.project_dir,
                            session_id=session_id,
                            task_id=task_id,
                            orchestration_state=orchestration_state,
                            trigger="repair_rejected",
                            related_event_id=phase_finished_event.get("event_id"),
                        )
                    except Exception:
                        pass
                    restore_workspace_snapshot_if_needed(reason)
                    write_project_state_snapshot_fn(db, project, task, session_id)
                    return {"status": "failed", "reason": reason}

                logger.info(
                    "[ORCHESTRATION] Applying %s before retrying step %s",
                    fix_type,
                    step_index + 1,
                )
                orchestration_state.debug_attempts.append(
                    {
                        "attempt": len(orchestration_state.debug_attempts) + 1,
                        "fix_type": fix_type,
                        "fix": debug_data.get("fix", ""),
                        "analysis": debug_data.get("analysis", ""),
                        "confidence": debug_data.get("confidence", "MEDIUM"),
                        "error": step_record.error_message,
                        "step_index": step_index,
                    }
                )
                step_updated = False
                if fix_type == "ops_fix" and isinstance(debug_data.get("ops"), list):
                    step["ops"] = debug_data.get("ops", [])
                    step["commands"] = []
                    step_updated = True
                if fix_type == "command_fix" and debug_data.get("fix"):
                    if not is_runnable_shell_command_fix(str(debug_data.get("fix"))):
                        logger.warning(
                            "[ORCHESTRATION] Ignoring non-runnable command_fix payload before retrying step %s",
                            step_index + 1,
                        )
                        fix_type = "code_fix"
                    else:
                        step["commands"] = [
                            debug_data.get(
                                "fix", step_commands[0] if step_commands else ""
                            )
                        ]
                        if structured_ops_present:
                            step["ops"] = []
                        step_updated = True
                if isinstance(debug_data.get("expected_files"), list):
                    step["expected_files"] = debug_data.get("expected_files", [])
                    step_updated = True
                if isinstance(debug_data.get("verification"), str):
                    verification_fix = debug_data.get("verification", "")
                    step["verification"] = verification_fix
                    if (
                        fix_type == "code_fix"
                        and verification_fix.strip()
                        and _verification_can_replace_stale_commands(step)
                    ):
                        step["commands"] = [verification_fix]
                    step_updated = True
                if step_updated:
                    orchestration_state.plan[step_index] = step
                    task.steps = json.dumps(orchestration_state.plan)
                emit_live(
                    "INFO",
                    f"[ORCHESTRATION] Fix applied ({fix_type}), retrying step {step_index + 1}",
                    metadata={
                        "phase": "debugging",
                        "step_index": step_index + 1,
                        "fix_type": fix_type,
                    },
                )
                save_orchestration_checkpoint(
                    db, session_id, task_id, prompt, orchestration_state
                )
                db.commit()
                try:
                    phase_finished_event = append_orchestration_event(
                        project_dir=orchestration_state.project_dir,
                        session_id=session_id,
                        task_id=task_id,
                        event_type=EventType.PHASE_FINISHED,
                        parent_event_id=(debugging_phase_event or {}).get("event_id"),
                        details={
                            "phase": "debugging",
                            "status": "retrying_step",
                            "step_index": step_index + 1,
                            "fix_type": fix_type,
                        },
                    )
                    write_orchestration_state_snapshot(
                        project_dir=orchestration_state.project_dir,
                        session_id=session_id,
                        task_id=task_id,
                        orchestration_state=orchestration_state,
                        trigger="phase_finished",
                        related_event_id=phase_finished_event.get("event_id"),
                    )
                except Exception:
                    pass
                continue

        except TaskOperationContractViolation as exc:
            orchestration_state.status = OrchestrationStatus.ABORTED
            orchestration_state.abort_reason = f"Operation contract violation: {exc}"
            mark_task_attempt_failed(
                task=task,
                session_task_link=session_task_link,
                task_execution=_get_task_execution(db, task_execution_id),
                error_message=str(exc),
                completed_at=datetime.now(timezone.utc),
            )
            db.commit()
            try:
                phase_finished_event = append_orchestration_event(
                    project_dir=orchestration_state.project_dir,
                    session_id=session_id,
                    task_id=task_id,
                    event_type=EventType.PHASE_FINISHED,
                    parent_event_id=(debugging_phase_event or {}).get("event_id"),
                    details={
                        "phase": "debugging",
                        "status": "op_contract_violation",
                        "step_index": step_index + 1,
                    },
                )
                write_orchestration_state_snapshot(
                    project_dir=orchestration_state.project_dir,
                    session_id=session_id,
                    task_id=task_id,
                    orchestration_state=orchestration_state,
                    trigger="phase_finished",
                    related_event_id=phase_finished_event.get("event_id"),
                )
            except Exception:
                pass
            restore_workspace_snapshot_if_needed("operation contract violation")
            return {"status": "failed", "reason": "op_contract_violation"}
        except workspace_violation_error_cls as exc:
            orchestration_state.status = OrchestrationStatus.ABORTED
            orchestration_state.abort_reason = f"Workspace isolation violation: {exc}"
            mark_task_attempt_failed(
                task=task,
                session_task_link=session_task_link,
                task_execution=_get_task_execution(db, task_execution_id),
                error_message=str(exc),
                completed_at=datetime.now(timezone.utc),
            )
            db.commit()
            try:
                phase_finished_event = append_orchestration_event(
                    project_dir=orchestration_state.project_dir,
                    session_id=session_id,
                    task_id=task_id,
                    event_type=EventType.PHASE_FINISHED,
                    parent_event_id=(debugging_phase_event or {}).get("event_id"),
                    details={
                        "phase": "debugging",
                        "status": "workspace_isolation_violation",
                        "step_index": step_index + 1,
                    },
                )
                write_orchestration_state_snapshot(
                    project_dir=orchestration_state.project_dir,
                    session_id=session_id,
                    task_id=task_id,
                    orchestration_state=orchestration_state,
                    trigger="phase_finished",
                    related_event_id=phase_finished_event.get("event_id"),
                )
            except Exception:
                pass
            restore_workspace_snapshot_if_needed("debug workspace isolation violation")
            return {"status": "failed", "reason": "workspace_isolation_violation"}
        except Exception as exc:
            logger.error("[ORCHESTRATION] Debug parsing failed: %s", exc)
            orchestration_state.status = OrchestrationStatus.ABORTED
            orchestration_state.abort_reason = f"Debug parse failed: {exc}"
            mark_task_attempt_failed(
                task=task,
                session_task_link=session_task_link,
                task_execution=_get_task_execution(db, task_execution_id),
                error_message=str(exc),
                completed_at=datetime.now(timezone.utc),
            )
            db.commit()
            try:
                phase_finished_event = append_orchestration_event(
                    project_dir=orchestration_state.project_dir,
                    session_id=session_id,
                    task_id=task_id,
                    event_type=EventType.PHASE_FINISHED,
                    parent_event_id=(debugging_phase_event or {}).get("event_id"),
                    details={
                        "phase": "debugging",
                        "status": "debug_parse_error",
                        "step_index": step_index + 1,
                    },
                )
                write_orchestration_state_snapshot(
                    project_dir=orchestration_state.project_dir,
                    session_id=session_id,
                    task_id=task_id,
                    orchestration_state=orchestration_state,
                    trigger="phase_finished",
                    related_event_id=phase_finished_event.get("event_id"),
                )
            except Exception:
                pass
            restore_workspace_snapshot_if_needed("debug parse error")
            return {"status": "failed", "reason": "debug_parse_error"}

    try:
        phase_finished_event = append_orchestration_event(
            project_dir=orchestration_state.project_dir,
            session_id=session_id,
            task_id=task_id,
            event_type=EventType.PHASE_FINISHED,
            parent_event_id=(executing_phase_event or {}).get("event_id"),
            details={"phase": "executing", "status": "completed"},
        )
        write_orchestration_state_snapshot(
            project_dir=orchestration_state.project_dir,
            session_id=session_id,
            task_id=task_id,
            orchestration_state=orchestration_state,
            trigger="phase_finished",
            related_event_id=phase_finished_event.get("event_id"),
        )
    except Exception:
        pass

    return {"status": "completed"}
