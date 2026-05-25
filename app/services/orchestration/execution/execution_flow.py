"""Execution-flow decision helpers for orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from .executor import ExecutorService
from .runtime import build_workspace_discovery_step
from ..types import ValidationVerdict
from ..validation.workspace_guard import (
    TaskWorkspaceViolationError,
    assert_no_workspace_cd_escape,
)
from ..validation.integrity import python_test_files
from ..validation.validator import ValidatorService


@dataclass
class StepExecutionAssessment:
    step_status: str
    step_output: str
    error_message: str
    missing_files: List[str]
    stub_files: List[str]
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


def _portable_relative_path(project_dir: Path, raw_path: str) -> Optional[Path]:
    path_text = str(raw_path or "").strip()
    if not path_text:
        return None
    candidate = Path(path_text)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        return None
    resolved = (project_dir / candidate).resolve()
    try:
        resolved.relative_to(project_dir.resolve())
    except ValueError:
        return None
    return resolved


def _execute_portable_posix_command(
    project_dir: Path, command: str
) -> Optional[Dict[str, Any]]:
    normalized = " ".join(str(command or "").strip().split())
    if not normalized:
        return None

    try:
        tokens = shlex.split(normalized, posix=True)
    except ValueError:
        return None

    if len(tokens) == 2 and tokens[0] == "cat":
        target = _portable_relative_path(project_dir, tokens[1])
        if target is None:
            return None
        if not target.is_file():
            return {
                "success": False,
                "command": command,
                "returncode": 1,
                "output": f"cat: {tokens[1]}: No such file",
            }
        return {
            "success": True,
            "command": command,
            "returncode": 0,
            "output": target.read_text(errors="replace")[:4000],
        }

    parts = [part.strip() for part in normalized.split("&&")]
    if not parts or not all(part for part in parts):
        return None

    outputs: List[str] = []
    for part in parts:
        try:
            part_tokens = shlex.split(part, posix=True)
        except ValueError:
            return None
        if len(part_tokens) == 3 and part_tokens[:2] == ["test", "-f"]:
            target = _portable_relative_path(project_dir, part_tokens[2])
            if target is None:
                return None
            if not target.is_file():
                return {
                    "success": False,
                    "command": command,
                    "returncode": 1,
                    "output": f"test: {part_tokens[2]}: file not found",
                }
            continue
        if len(part_tokens) >= 2 and part_tokens[0] == "echo":
            outputs.append(" ".join(part_tokens[1:]))
            continue
        return None

    return {
        "success": True,
        "command": command,
        "returncode": 0,
        "output": "\n".join(outputs),
    }


def _python_test_files_exist(project_dir: Path) -> bool:
    return bool(python_test_files(project_dir))


def _verification_success(
    *,
    project_dir: Path,
    raw_command: str,
    returncode: int,
    output: str,
) -> bool:
    if returncode != 0:
        return False
    normalized = " ".join(raw_command.strip().split())
    if "unittest" in normalized and "NO TESTS RAN" in output.upper():
        return not _python_test_files_exist(project_dir)
    return True


# Minimum non-trivial file size in bytes.  Files smaller than this are
# treated as "empty" (touch, echo '', placeholder stubs).
_MIN_MEANINGFUL_BYTES = 4
_GLOB_META_CHARS = {"*", "?", "["}


def _is_allowed_empty_sentinel_file(path: Path) -> bool:
    return path.name == ".gitkeep"


def _resolve_expected_path(
    project_dir: Path, raw_path: str
) -> Optional[tuple[Path, str]]:
    """
    Resolve one raw expected_files entry to (full_path, display_label).

    Handles both relative paths and absolute paths that include the project_dir
    prefix (which the planner sometimes emits). Returns None if the entry is blank.
    """
    path_text = str(raw_path or "").strip().strip("'\"\\")
    if not path_text:
        return None
    candidate = Path(path_text)
    if candidate.is_absolute():
        # Strip project_dir prefix if present so the path resolves correctly.
        try:
            rel = candidate.relative_to(project_dir)
            return project_dir / rel, str(rel)
        except ValueError:
            # Absolute path that is NOT under project_dir — use as-is.
            return candidate, path_text
    return project_dir / path_text, path_text


def _expand_expected_file_entry(
    project_dir: Path, raw_path: str
) -> tuple[list[tuple[Path, str]], str | None]:
    path_text = str(raw_path or "").strip().strip("'\"\\")
    if not path_text:
        return [], None
    if not any(char in path_text for char in _GLOB_META_CHARS):
        resolved = _resolve_expected_path(project_dir, path_text)
        return ([resolved] if resolved else []), path_text
    candidate = Path(path_text)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        resolved = _resolve_expected_path(project_dir, path_text)
        return ([resolved] if resolved else []), path_text
    matches: list[tuple[Path, str]] = []
    for match in sorted(project_dir.glob(path_text)):
        try:
            rel = match.resolve().relative_to(project_dir.resolve())
        except ValueError:
            continue
        if match.is_file():
            matches.append((match, rel.as_posix()))
    return matches, path_text


def missing_expected_files(project_dir: Path, expected_files: List[str]) -> List[str]:
    """Return expected paths that are truly absent (do not exist on disk at all)."""
    missing: List[str] = []
    for raw_path in expected_files or []:
        resolved_entries, label = _expand_expected_file_entry(project_dir, raw_path)
        if not label:
            continue
        if not resolved_entries:
            missing.append(label)
            continue
        if not any(full_path.exists() for full_path, _ in resolved_entries):
            missing.append(label)
        # Directories are not deliverable files — skip silently.
    return missing


def stub_expected_files(project_dir: Path, expected_files: List[str]) -> List[str]:
    """
    Return expected paths that exist on disk but are empty/stub (no real content).

    A file is a stub if it:
      - exists but is zero bytes, OR
      - exists but is smaller than _MIN_MEANINGFUL_BYTES AND contains only
        whitespace / comment lines (catches `# TODO` stubs).
    """
    stubs: List[str] = []
    for raw_path in expected_files or []:
        resolved_entries, _ = _expand_expected_file_entry(project_dir, raw_path)
        if not resolved_entries:
            continue
        for full_path, label in resolved_entries:
            if not full_path.exists() or full_path.is_dir():
                continue
            if _is_allowed_empty_sentinel_file(full_path):
                continue
            size = full_path.stat().st_size
            if size == 0:
                stubs.append(label)
                continue
            if size < _MIN_MEANINGFUL_BYTES:
                try:
                    content = full_path.read_text(errors="replace").strip()
                    if not content or all(
                        line.strip().startswith("#") or not line.strip()
                        for line in content.splitlines()
                    ):
                        stubs.append(label)
                except OSError:
                    pass
    return stubs


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
        assert_no_workspace_cd_escape(raw_command, project_dir)
    except TaskWorkspaceViolationError as exc:
        return {
            "success": False,
            "command": raw_command,
            "returncode": 2,
            "output": str(exc),
        }

    portable_result = _execute_portable_posix_command(project_dir, raw_command)
    if portable_result is not None:
        return portable_result

    command_to_run = raw_command
    if raw_command in {"python", "python3"}:
        command_to_run = subprocess.list2cmdline([sys.executable])
    elif raw_command.startswith("python "):
        command_to_run = (
            subprocess.list2cmdline([sys.executable]) + raw_command[len("python") :]
        )
    elif raw_command.startswith("python3 "):
        command_to_run = (
            subprocess.list2cmdline([sys.executable]) + raw_command[len("python3") :]
        )

    try:
        env = os.environ.copy()
        python_dir = str(Path(sys.executable).parent)
        env["PATH"] = python_dir + os.pathsep + env.get("PATH", "")
        completed = subprocess.run(
            command_to_run,
            cwd=str(project_dir),
            env=env,
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
        if completed.returncode != 0 and not output:
            output = (
                "Verification command failed with return code "
                f"{completed.returncode}: {raw_command}"
            )
        success = _verification_success(
            project_dir=project_dir,
            raw_command=raw_command,
            returncode=completed.returncode,
            output=output,
        )
        return {
            "success": success,
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


def patch_python_verification_imports(command: str) -> str:
    """Patch common model-generated python -c verification import omissions."""
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
    imports_sys = bool(re.search(r"(^|;)\s*import\s+[^;]*\bsys\b", script))
    needs_sys = "sys." in script and not imports_sys
    stdlib_imports = {
        "json",
        "os",
        "pathlib",
        "re",
        "shlex",
        "subprocess",
        "sys",
        "typing",
        "unittest",
    }
    imported_modules: set[str] = set()
    for match in re.finditer(r"(^|;)\s*import\s+([^;]+)", script):
        for raw_name in match.group(2).split(","):
            name = raw_name.strip().split()[0].split(".")[0]
            if name:
                imported_modules.add(name)
    for match in re.finditer(r"(^|;)\s*from\s+([A-Za-z_][A-Za-z0-9_]*)\b", script):
        imported_modules.add(match.group(2))
    may_import_backend_module = bool(imported_modules - stdlib_imports) and (
        "backend" not in script
    )
    if needs_sys or may_import_backend_module:
        prefix = ""
        if needs_sys or may_import_backend_module:
            prefix += "import sys; "
        if may_import_backend_module:
            prefix += "'backend' in sys.path or sys.path.append('backend'); "
        script = prefix + script
        return f"{tokens[0]} -c {shlex.quote(script)}"
    return command


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
    if step_result.get(
        "skip_declared_verification"
    ) and ValidatorService._step_is_readonly_inspection(step):
        expected_files = []
    stub_files: List[str] = []

    if step_status == "success":
        missing_files = missing_expected_files(project_dir, expected_files)
        stub_files = stub_expected_files(project_dir, expected_files)
        if missing_files:
            step_status = "failed"
            missing_summary = ", ".join(missing_files[:6])
            error_message = (
                "Step reported success but expected files are missing: "
                f"{missing_summary}"
            )
            if stub_files:
                stub_summary = ", ".join(stub_files[:6])
                error_message += (
                    f". Note: these files EXIST but are empty/stub and need content: "
                    f"{stub_summary}. Write actual content, do not just recreate paths."
                )
        elif stub_files:
            # Files exist on disk but have no real content — distinct from missing.
            step_status = "failed"
            stub_summary = ", ".join(stub_files[:6])
            correction_hints = ExecutorService.stub_file_repair_hints(
                project_dir,
                stub_files,
                str(step.get("verification") or ""),
            )
            error_message = (
                "Step produced empty or stub files that contain no real content: "
                f"{stub_summary}. "
                "The files EXIST on disk — write their bodies, do not recreate paths."
            )
            if correction_hints:
                error_message += " | Retry hints: " + " | ".join(correction_hints[:3])

    structured_ops_present = isinstance(step.get("ops"), list) and any(
        isinstance(operation, dict)
        and str(operation.get("op") or "").strip()
        in {"write_file", "append_file", "replace_in_file"}
        for operation in step.get("ops", []) or []
    )
    commands_present = any(
        str(command or "").strip() for command in step.get("commands", []) or []
    )
    tool_failures = []
    if not (structured_ops_present and not commands_present):
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
    if (
        step_status == "success"
        and verification_command
        and not step_result.get("skip_declared_verification")
    ):
        verification_command = patch_python_verification_imports(verification_command)
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
            reported_changed_files=step_result.get("files_changed", []),
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
        stub_files=stub_files,
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
