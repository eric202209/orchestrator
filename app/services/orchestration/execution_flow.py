"""Execution-flow decision helpers for orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import subprocess
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from .executor import ExecutorService
from .runtime import build_workspace_discovery_step
from .types import ValidationVerdict
from .validator import ValidatorService


@dataclass
class StepExecutionAssessment:
    step_status: str
    step_output: str
    error_message: str
    missing_files: List[str]
    tool_failures: List[str]
    correction_hints: List[str]
    verification_output: str = ""
    validation_verdict: Optional[ValidationVerdict] = None


@dataclass
class ToolPathFailureDecision:
    action: str  # rewrite_step | manual_review | none
    message: str = ""
    rewritten_step: Optional[Dict[str, Any]] = None


def is_long_running_verification_task(
    execution_profile: str, step_description: str, task_prompt: str
) -> bool:
    combined = f"{execution_profile} {step_description} {task_prompt}".lower()
    verification_markers = (
        "verify",
        "verification",
        "refine",
        "integration",
        "end-to-end",
        "e2e",
        "test",
        "qa",
        "audit",
        "review",
        "build",
    )
    return execution_profile in {"test_only", "review_only"} or any(
        marker in combined for marker in verification_markers
    )


def determine_step_timeout(
    *,
    timeout_seconds: int,
    total_steps: int,
    execution_profile: str,
    step_description: str,
    task_prompt: str,
) -> int:
    if is_long_running_verification_task(
        execution_profile, step_description, task_prompt
    ):
        return max(600, min(timeout_seconds, 1800))
    return max(300, timeout_seconds // max(1, min(total_steps, 3)))


# Minimum non-trivial file size in bytes.  Files smaller than this are
# treated as "empty" (touch, echo '', placeholder stubs).
_MIN_MEANINGFUL_BYTES = 4


def missing_expected_files(project_dir: Path, expected_files: List[str]) -> List[str]:
    """
    Return the list of expected paths that are absent OR effectively empty.

    A file is considered missing if it:
      - does not exist, OR
      - exists but is zero bytes, OR
      - exists but is smaller than _MIN_MEANINGFUL_BYTES AND contains only
        whitespace / comment lines (catches `# TODO` stubs).
    """
    missing: List[str] = []
    for raw_path in expected_files or []:
        path_text = str(raw_path or "").strip().strip("'\"\\")
        if not path_text:
            continue

        full_path = project_dir / path_text

        if not full_path.exists():
            missing.append(path_text)
            continue

        # Directories are not deliverable files — skip silently.
        if full_path.is_dir():
            continue

        size = full_path.stat().st_size
        if size == 0:
            missing.append(path_text)
            continue

        if size < _MIN_MEANINGFUL_BYTES:
            try:
                content = full_path.read_text(errors="replace").strip()
                if not content or all(
                    line.strip().startswith("#") or not line.strip()
                    for line in content.splitlines()
                ):
                    missing.append(path_text)
            except OSError:
                pass  # Unreadable → treat as present; execution will surface error

    return missing


def empty_expected_files(project_dir: Path, expected_files: List[str]) -> List[str]:
    """Return files that exist but are empty/stub — useful for debug messages."""
    present_but_empty: List[str] = []
    for raw_path in expected_files or []:
        path_text = str(raw_path or "").strip().strip("'\"\\")
        if not path_text:
            continue
        full_path = project_dir / path_text
        if not full_path.exists() or full_path.is_dir():
            continue
        if full_path.stat().st_size < _MIN_MEANINGFUL_BYTES:
            present_but_empty.append(path_text)
    return present_but_empty


def execute_verification_command(
    *,
    project_dir: Path,
    command: str,
    timeout_seconds: int = 120,
) -> Dict[str, Any]:
    raw_command = str(command or "").strip()
    if not raw_command:
        return {
            "success": True,
            "command": raw_command,
            "returncode": 0,
            "output": "",
        }

    try:
        completed = subprocess.run(
            raw_command,
            cwd=str(project_dir),
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        output = "\n".join(
            part
            for part in [completed.stdout.strip(), completed.stderr.strip()]
            if part
        ).strip()
        return {
            "success": completed.returncode == 0,
            "command": raw_command,
            "returncode": completed.returncode,
            "output": output[:4000],
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "command": raw_command,
            "returncode": None,
            "output": f"Verification command timed out after {timeout_seconds}s",
        }


def assess_step_execution(
    *,
    db: Session,
    session_id: int,
    task_id: int,
    project_dir: Path,
    step: Dict[str, Any],
    step_result: Dict[str, Any],
    step_started_at: datetime,
    validation_profile: str,
    validation_severity: str = "standard",
    relaxed_mode: bool = False,
) -> StepExecutionAssessment:
    step_output = str(step_result.get("output", ""))
    step_status = "success" if step_result.get("status") != "failed" else "failed"
    error_message = str(step_result.get("error", ""))
    missing_files: List[str] = []
    tool_failures: List[str] = []
    correction_hints: List[str] = []
    validation_verdict: Optional[ValidationVerdict] = None
    verification_output = str(step_result.get("verification_output", "") or "")

    expected_files = step.get("expected_files", []) or []

    if step_status == "success":
        missing_files = missing_expected_files(project_dir, expected_files)
        if missing_files:
            step_status = "failed"
            missing_summary = ", ".join(missing_files[:6])
            error_message = (
                "Step reported success but expected files are missing: "
                f"{missing_summary}"
            )
        # Surface empty/stub files as a distinct failure so the debug
        # prompt knows to re-generate content, not just re-create paths.
        empty_files = empty_expected_files(project_dir, expected_files)
        if empty_files and not missing_files:
            step_status = "failed"
            empty_summary = ", ".join(empty_files[:6])
            error_message = (
                "Step produced empty or stub files that contain no real content: "
                f"{empty_summary}. "
                "Re-generate the file bodies — do not just recreate empty paths."
            )

    tool_failures = ExecutorService.recent_step_tool_failures(
        db,
        session_id,
        task_id,
        step_started_at,
    )
    if step_status == "success" and tool_failures:
        step_status = "failed"
        failure_summary = " | ".join(tool_failures[:3])
        correction_hints = ExecutorService.tool_failure_correction_hints(
            tool_failures, project_dir
        )
        error_message = (
            "Step reported success but task logs contain tool failures: "
            f"{failure_summary}"
        )
        if correction_hints:
            error_message += " | Retry hints: " + " | ".join(correction_hints[:3])

    verification_command = str(step.get("verification") or "").strip()
    if step_status == "success" and verification_command:
        verification_result = execute_verification_command(
            project_dir=project_dir,
            command=verification_command,
        )
        verification_output = verification_result.get("output", "")
        step_result["verification_output"] = verification_output
        if not verification_result.get("success", False):
            step_status = "failed"
            error_message = (
                "Step verification command failed"
                f" (`{verification_command}`): {verification_output[:500]}"
            )

    if step_status == "success":
        validation_verdict = ValidatorService.validate_step_success(
            project_dir=project_dir,
            step=step,
            step_output=step_output,
            missing_expected_files=missing_files,
            tool_failures=tool_failures,
            validation_profile=validation_profile,
            validation_severity=validation_severity,
            relaxed_mode=relaxed_mode,
        )
        if not validation_verdict.accepted:
            step_status = "failed"
            error_message = "Step failed implementation validation: " + " | ".join(
                validation_verdict.reasons[:3]
            )

    return StepExecutionAssessment(
        step_status=step_status,
        step_output=step_output,
        error_message=error_message,
        missing_files=missing_files,
        tool_failures=tool_failures,
        correction_hints=correction_hints,
        verification_output=verification_output,
        validation_verdict=validation_verdict,
    )


def repeated_tool_path_failure_decision(
    *,
    step_index: int,
    execution_profile: str,
    validation_profile: str,
    expected_files: List[str],
    step: Dict[str, Any],
    project_dir: Path,
    error_message: str,
    relaxed_mode: bool = False,
) -> ToolPathFailureDecision:
    read_only_step = (
        execution_profile in {"review_only", "test_only"}
        or validation_profile != "implementation"
        or not expected_files
    )
    if read_only_step:
        return ToolPathFailureDecision(
            action="rewrite_step",
            message=(
                f"Step {step_index + 1} hit repeated workspace/tool-path failures, "
                "so the step was rewritten into a workspace-discovery inspection step "
                "instead of forcing manual review"
            ),
            rewritten_step=build_workspace_discovery_step(
                step, project_dir, error_message
            ),
        )
    if relaxed_mode:
        return ToolPathFailureDecision(
            action="rewrite_step",
            message=(
                f"Step {step_index + 1} hit repeated workspace/tool-path failures, "
                "so relaxed mode rewrote it into a workspace-discovery step before giving up"
            ),
            rewritten_step=build_workspace_discovery_step(
                step, project_dir, error_message
            ),
        )
    return ToolPathFailureDecision(
        action="manual_review",
        message=(
            f"Step {step_index + 1} hit repeated workspace/tool-path failures. "
            "Manual review is required before execution can continue."
        ),
    )
