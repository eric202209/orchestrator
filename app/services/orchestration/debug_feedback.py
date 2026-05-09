"""Phase 7F debug feedback envelope helpers.

These helpers capture execution/completion failures as structured evidence.
They do not decide retry policy or execute repair; orchestration callers remain
responsible for those control decisions.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from app.models import LogEntry
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.persistence import append_orchestration_event
from app.services.workspace.path_display import render_workspace_path_for_prompt

ELIGIBLE_DEBUG_FAILURE_CLASSES = frozenset(
    {
        "pytest_failure",
        "import_error",
        "module_not_found",
        "runtime_assertion_failure",
        "completion_validation_failed",
        "missing_dependency",
        "syntax_error",
    }
)


@dataclass
class DebugFeedbackEnvelope:
    """Structured runtime failure evidence for bounded debug repair."""

    task_execution_id: Optional[int]
    task_id: int
    step_index: Optional[int]
    failure_phase: str
    failed_command: str = ""
    return_code: Optional[int] = None
    stdout_excerpt: str = ""
    stderr_excerpt: str = ""
    pytest_excerpt: str = ""
    validator_reasons: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    workspace_path: str = ""
    failure_class: str = "unknown"
    schema_version: int = 1

    @property
    def eligible_for_debug_repair(self) -> bool:
        return self.failure_class in ELIGIBLE_DEBUG_FAILURE_CLASSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_execution_id": self.task_execution_id,
            "task_id": self.task_id,
            "step_index": self.step_index,
            "failure_phase": self.failure_phase,
            "failed_command": self.failed_command,
            "return_code": self.return_code,
            "stdout_excerpt": self.stdout_excerpt,
            "stderr_excerpt": self.stderr_excerpt,
            "pytest_excerpt": self.pytest_excerpt,
            "validator_reasons": list(self.validator_reasons),
            "changed_files": list(self.changed_files),
            "workspace_path": self.workspace_path,
            "failure_class": self.failure_class,
            "eligible_for_debug_repair": self.eligible_for_debug_repair,
        }


def _excerpt(value: Any, max_chars: int = 1200) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _first_pytest_excerpt(text: str) -> str:
    lines = str(text or "").splitlines()
    selected: list[str] = []
    markers = (
        "failed",
        "error",
        "assert",
        "traceback",
        "expected",
        "received",
        "no module named",
        "module not found",
    )
    for line in lines:
        lowered = line.lower()
        if any(marker in lowered for marker in markers):
            selected.append(line[:240])
        if len(selected) >= 12:
            break
    return _excerpt("\n".join(selected), 1200)


def classify_debug_failure(
    *,
    failed_command: str = "",
    stdout: str = "",
    stderr: str = "",
    validator_reasons: Optional[Iterable[str]] = None,
    return_code: Optional[int] = None,
) -> str:
    """Map runtime evidence to the Phase 7F eligible failure taxonomy."""

    reasons = "\n".join(str(reason or "") for reason in (validator_reasons or []))
    combined = "\n".join(
        part
        for part in [failed_command, stdout, stderr, reasons, str(return_code or "")]
        if str(part or "").strip()
    ).lower()
    if not combined:
        return "unknown"

    if "syntaxerror" in combined or "syntax error" in combined:
        return "syntax_error"
    if "modulenotfounderror" in combined or "no module named" in combined:
        return "module_not_found"
    if "importerror" in combined or "cannot import name" in combined:
        return "import_error"
    if (
        "command not found" in combined
        or "not found" in combined
        and any(tool in combined for tool in ("pytest", "jest", "vitest", "mocha"))
        or "cannot find module" in combined
    ):
        return "missing_dependency"
    if "pytest" in combined and (
        " failed" in combined
        or " failures" in combined
        or "failed tests" in combined
        or "== fail" in combined
        or "assertionerror" in combined
        or re.search(r"\bassert\b", combined)
    ):
        return "pytest_failure"
    if "assertionerror" in combined or re.search(r"\bassert\b", combined):
        return "runtime_assertion_failure"
    if "completion_validation_failed" in combined or (
        "completion" in combined and "validation" in combined
    ):
        return "completion_validation_failed"
    return "unknown"


def build_debug_feedback_envelope(
    *,
    task_execution_id: Optional[int],
    task_id: int,
    step_index: Optional[int],
    failure_phase: str,
    failed_command: str = "",
    return_code: Optional[int] = None,
    stdout: str = "",
    stderr: str = "",
    validator_reasons: Optional[Iterable[str]] = None,
    changed_files: Optional[Iterable[str]] = None,
    workspace_path: Any = "",
) -> DebugFeedbackEnvelope:
    reasons = [str(reason) for reason in (validator_reasons or []) if str(reason)]
    stdout_excerpt = _excerpt(stdout)
    stderr_excerpt = _excerpt(stderr)
    combined_output = "\n".join(part for part in [stdout, stderr, *reasons] if part)
    return DebugFeedbackEnvelope(
        task_execution_id=task_execution_id,
        task_id=task_id,
        step_index=step_index,
        failure_phase=failure_phase,
        failed_command=str(failed_command or ""),
        return_code=return_code,
        stdout_excerpt=stdout_excerpt,
        stderr_excerpt=stderr_excerpt,
        pytest_excerpt=_first_pytest_excerpt(combined_output),
        validator_reasons=reasons[:10],
        changed_files=[str(path) for path in (changed_files or []) if str(path)][:20],
        workspace_path=str(workspace_path or ""),
        failure_class=classify_debug_failure(
            failed_command=failed_command,
            stdout=stdout_excerpt,
            stderr=stderr_excerpt,
            validator_reasons=reasons,
            return_code=return_code,
        ),
    )


def persist_debug_feedback_envelope(
    *,
    db: Session,
    session_id: int,
    task_id: int,
    session_instance_id: Optional[str],
    project_dir: Any,
    envelope: DebugFeedbackEnvelope,
    parent_event_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Store debug feedback in LogEntry metadata and the event journal."""

    payload = envelope.to_dict()
    metadata = {
        "event_type": EventType.DEBUG_FEEDBACK_CAPTURED,
        "debug_feedback_captured": True,
        "debug_failure_class": envelope.failure_class,
        "debug_repair_used": False,
        "debug_repair_attempted": False,
        "debug_feedback_envelope": payload,
        "task_execution_id": envelope.task_execution_id,
        "task_id": task_id,
    }
    db.add(
        LogEntry(
            session_id=session_id,
            task_id=task_id,
            task_execution_id=envelope.task_execution_id,
            level="WARN",
            message=(
                "[ORCHESTRATION] Debug feedback captured "
                f"({envelope.failure_phase}:{envelope.failure_class})"
            ),
            log_metadata=json.dumps(metadata),
            session_instance_id=session_instance_id,
        )
    )
    db.flush()

    try:
        return append_orchestration_event(
            project_dir=project_dir,
            session_id=session_id,
            task_id=task_id,
            event_type=EventType.DEBUG_FEEDBACK_CAPTURED,
            parent_event_id=parent_event_id,
            details={
                **metadata,
                "phase": (
                    "completion"
                    if envelope.failure_phase.startswith("completion")
                    else "execution"
                ),
                "eligible_for_debug_repair": envelope.eligible_for_debug_repair,
            },
        )
    except Exception:
        return None


def build_bounded_debug_repair_prompt(envelope: DebugFeedbackEnvelope) -> str:
    """Render the bounded Phase 7F debug repair prompt body."""

    workspace = render_workspace_path_for_prompt(Path(envelope.workspace_path or "."))
    excerpts = {
        "stdout_excerpt": envelope.stdout_excerpt,
        "stderr_excerpt": envelope.stderr_excerpt,
        "pytest_excerpt": envelope.pytest_excerpt,
    }
    return (
        "Return a bare JSON array of one minimal debug repair step. "
        "Do not return prose, markdown, comments, explanations, or fenced code.\n\n"
        f"Workspace scope: {workspace}\n"
        f"Failure class: {envelope.failure_class}\n"
        f"Failed command: {envelope.failed_command or '(none recorded)'}\n"
        f"Return code: {envelope.return_code}\n"
        "Validator reasons:\n"
        f"{json.dumps(envelope.validator_reasons[:8], ensure_ascii=True)}\n"
        "Failure excerpts:\n"
        f"{json.dumps(excerpts, ensure_ascii=True)[:1800]}\n\n"
        "Rules:\n"
        "1. Output exactly one JSON array containing one step object.\n"
        "2. The step object must include title, command, and verification_command.\n"
        "3. Keep the fix atomic; do not rewrite unrelated files.\n"
        "4. Use relative paths only; no absolute paths, `..`, or `~`.\n"
        "5. Do not bypass validators, workspace boundaries, or verification.\n"
        "6. Do not request additional retries or describe policy.\n"
    )


def normalize_bounded_debug_repair_payload(
    parsed_data: Any,
) -> Optional[dict[str, Any]]:
    """Convert a Phase 7F repair array into the legacy debug action shape."""

    if not isinstance(parsed_data, list) or len(parsed_data) != 1:
        return None
    item = parsed_data[0]
    if not isinstance(item, dict):
        return None

    command = str(item.get("command") or "").strip()
    verification = str(item.get("verification_command") or "").strip()
    if not command or not verification:
        return None

    expected_files = item.get("expected_files", [])
    if isinstance(expected_files, str):
        expected_files = [expected_files]
    if not isinstance(expected_files, list):
        expected_files = []

    return {
        "fix_type": "command_fix",
        "fix": command,
        "analysis": str(item.get("title") or "Apply bounded debug repair")[:1200],
        "confidence": "MEDIUM",
        "verification": verification,
        "expected_files": [
            str(path).strip() for path in expected_files if str(path).strip()
        ],
    }
