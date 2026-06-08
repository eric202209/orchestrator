"""Incremental execution prototype — Slice J.

Creation-only path: parse description → generate content (1 LLM call) →
write file → run verify command.

Falls back to full planning on any failure without consuming retry budget or
mutating orchestration_state.plan.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.state.persistence import append_orchestration_event
from app.services.prompt_templates import StepResult

# Code-fence stripper: LLMs often wrap output in ``` blocks or add prose before
# the block despite the prompt saying "raw content only."  Search anywhere in the
# output so a leading prose sentence doesn't prevent extraction.
_CODE_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)


def _strip_code_fences(content: str) -> str:
    m = _CODE_FENCE_RE.search(content)
    if m:
        return m.group(1).strip()
    return content


# Verify-command extraction patterns.
_VERIFY_WITH_RE = re.compile(r"[Vv]erify with\s+(.+?)[\s.]*$", re.MULTILINE)
_VERIFY_EXISTS_RE = re.compile(r"[Vv]erify.*\bexists\b", re.IGNORECASE)
_VERIFY_VALID_PYTHON_RE = re.compile(r"[Vv]erify.*\bvalid [Pp]ython\b", re.IGNORECASE)


def _parse_verify_command(description: str, file_paths: List[str]) -> Optional[str]:
    """Extract an executable verify command from the task description.

    Returns None if no recognised pattern is found (caller must fall back).
    """
    # "Verify with <command>"
    m = _VERIFY_WITH_RE.search(description)
    if m:
        return m.group(1).strip().rstrip(".")
    # "Verify it exists" / "Verify the file exists" etc.
    if _VERIFY_EXISTS_RE.search(description):
        target = file_paths[0] if file_paths else None
        if target:
            return f"test -f {target}"
    # "Verify it is valid Python" / "Verify the file is valid Python"
    if _VERIFY_VALID_PYTHON_RE.search(description):
        target = file_paths[0] if file_paths else None
        if target:
            return f"python3 -m py_compile {target}"
    return None


def _is_within_project_dir(file_path: str, project_dir: Path) -> bool:
    """Return True if file_path resolves to a location inside project_dir."""
    try:
        resolved = (project_dir / file_path).resolve()
        return str(resolved).startswith(str(project_dir.resolve()))
    except Exception:
        return False


def _emit_event(ctx: Any, event_type: str, details: Dict[str, Any]) -> None:
    try:
        append_orchestration_event(
            project_dir=ctx.orchestration_state.project_dir,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            event_type=event_type,
            details=details,
        )
    except Exception:
        pass


def _fallback(ctx: Any, reason: str, phase: str) -> Dict[str, Any]:
    """Emit fallback event and return a failed-status dict."""
    ctx.logger.info(
        "[ORCHESTRATION] INCREMENTAL fallback: task_id=%s reason=%s",
        ctx.task_id,
        reason,
    )
    _emit_event(
        ctx,
        EventType.INCREMENTAL_FALLBACK_TO_PLANNING,
        {
            "task_id": ctx.task_id,
            "reason": reason,
            "fallback_at_phase": phase,
        },
    )
    return {"status": "failed", "reason": reason}


def attempt_incremental_execution(
    *,
    ctx: Any,
    task_description: str,
) -> Dict[str, Any]:
    """Attempt direct write + verify for a creation-only task.

    Contract:
    - orchestration_state.plan is NOT mutated unless the attempt succeeds.
    - On success: plan is populated with a synthetic step, execution_results
      is populated, current_step_index == len(plan), returns {"status": "completed"}.
    - On any failure: plan remains empty, returns {"status": "failed", "reason": ...}.
    - Retry budget (debug_attempts) is never modified.
    """
    from app.services.orchestration.planning.incremental_classifier import (
        _extract_file_paths,
    )

    orchestration_state = ctx.orchestration_state
    task_id = ctx.task_id

    file_paths = _extract_file_paths(task_description)
    if not file_paths:
        return _fallback(ctx, "classifier_error", "path_extraction")

    project_dir = Path(orchestration_state.project_dir)

    # Safety: all paths must resolve inside project_dir.
    for fp in file_paths:
        if not _is_within_project_dir(fp, project_dir):
            ctx.logger.warning(
                "[ORCHESTRATION] INCREMENTAL fallback: task_id=%s reason=path_outside_project fp=%s",
                task_id,
                fp,
            )
            return _fallback(ctx, "path_outside_project", "safety_check")

    verify_cmd = _parse_verify_command(task_description, file_paths)
    if verify_cmd is None:
        return _fallback(ctx, "unparseable_verify", "verify_parse")

    ctx.logger.info(
        "[ORCHESTRATION] INCREMENTAL candidate: task_id=%s desc_chars=%s files=%s",
        task_id,
        len(task_description),
        len(file_paths),
    )
    _emit_event(
        ctx,
        EventType.INCREMENTAL_ATTEMPTED,
        {
            "task_id": task_id,
            "description_chars": len(task_description),
            "file_count": len(file_paths),
        },
    )

    # Generate file content via single LLM call.
    primary_file = file_paths[0]
    prompt = (
        f"Generate the content for the following file creation task.\n\n"
        f"Task: {task_description}\n"
        f"File to create: {primary_file}\n\n"
        f"Output ONLY the raw file content. No code fences, no explanation, "
        f"no markdown. Start directly with the content."
    )
    try:
        result = asyncio.run(
            ctx.runtime_service.execute_task(prompt, timeout_seconds=240)
        )
    except Exception as exc:
        return _fallback(ctx, f"exception:{type(exc).__name__}", "content_generation")

    content = _strip_code_fences((result.get("output") or "").strip())
    if not content:
        return _fallback(ctx, "content_empty", "content_generation")

    # Write file — parent directories are created as needed.
    target_path = project_dir / primary_file
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding="utf-8")
    except Exception:
        return _fallback(ctx, "write_failed", "file_write")

    # Run verify command as subprocess.
    try:
        proc = subprocess.run(
            verify_cmd,
            shell=True,
            capture_output=True,
            text=True,
            cwd=str(project_dir),
            timeout=30,
        )
        verify_exit_code = proc.returncode
        verify_stdout = proc.stdout[:2000]
    except Exception:
        return _fallback(ctx, "verify_failed", "verify_execution")

    if verify_exit_code != 0:
        ctx.logger.info(
            "[ORCHESTRATION] INCREMENTAL fallback: task_id=%s reason=verify_failed exit_code=%s",
            task_id,
            verify_exit_code,
        )
        _emit_event(
            ctx,
            EventType.INCREMENTAL_FALLBACK_TO_PLANNING,
            {
                "task_id": task_id,
                "reason": "verify_failed",
                "fallback_at_phase": "verify_result",
                "exit_code": verify_exit_code,
            },
        )
        return {"status": "failed", "reason": "verify_failed"}

    # Success — populate orchestration_state with a synthetic plan and result.
    # orchestration_state.plan was not touched before this point.
    synthetic_step = {
        "step_number": 1,
        "description": f"Create {primary_file} with specified content and verify",
        "commands": [verify_cmd],
        "expected_files": list(file_paths),
        "verification": verify_cmd,
    }
    orchestration_state.plan = [synthetic_step]
    step_result = StepResult(
        step_number=1,
        status="success",
        output=f"Created {primary_file}; verify exit code {verify_exit_code}",
        verification_output=verify_stdout,
        files_changed=list(file_paths),
        attempt=1,
    )
    orchestration_state.record_success(step_result)

    ctx.logger.info(
        "[ORCHESTRATION] INCREMENTAL succeeded: task_id=%s llm_calls=1",
        task_id,
    )
    _emit_event(
        ctx,
        EventType.INCREMENTAL_SUCCEEDED,
        {
            "task_id": task_id,
            "llm_calls_used": 1,
            "verify_exit_code": verify_exit_code,
        },
    )
    return {"status": "completed"}
