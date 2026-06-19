"""Phase 13B-S1: Compact evidence packet for bounded execution recovery.

Contains only the dataclass and factory helpers. No LLM calls are made here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

_TRACEBACK_RE = re.compile(
    r"(Traceback \(most recent call last\):.*?(?:(?:Error|Exception|Warning)[^\n]*\n))",
    re.DOTALL,
)

_MAX_STDOUT = 800
_MAX_STDERR = 1200
_MAX_TRACEBACK = 600
_MAX_DESCRIPTION = 400
_MAX_COMMAND = 400
_MAX_VALIDATOR_REASON = 400


def _extract_traceback(text: str) -> str:
    if not text:
        return ""
    match = _TRACEBACK_RE.search(text)
    if match:
        return match.group(1).strip()
    for line in text.splitlines():
        if re.search(r"\b(?:Error|Exception|Warning):", line):
            return line.strip()[:_MAX_TRACEBACK]
    return ""


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


@dataclass
class ExecutionRecoveryEvidence:
    """Compact evidence packet assembled before an execution recovery attempt.

    Built entirely from already-available runtime data — no LLM calls required.
    Maximum total size: ~6000 chars (enforced by field-level truncation).
    """

    # Task identity
    task_title: str
    task_description: str

    # Failure location
    failed_command: str
    exit_code: Optional[int]

    # Output (pre-truncated to field caps)
    stdout_excerpt: str
    stderr_excerpt: str
    traceback_excerpt: str

    # Workspace state
    changed_files: List[str] = field(default_factory=list)
    git_diff_summary: str = ""  # populated in Phase 13B-full
    requested_symbols: List[str] = field(default_factory=list)  # from 10K-c

    # Guards (read-only context, not applied by recovery)
    active_human_guidance: List[str] = field(default_factory=list)  # 13B-full
    validator_rejection_reason: str = ""

    # Derived
    failure_class: str = "unknown"

    @property
    def is_empty(self) -> bool:
        """True when no actionable evidence exists for a recovery patch."""
        return not any(
            [
                self.stdout_excerpt.strip(),
                self.stderr_excerpt.strip(),
                self.traceback_excerpt.strip(),
                self.git_diff_summary.strip(),
            ]
        )

    @property
    def total_chars(self) -> int:
        return (
            len(self.stdout_excerpt)
            + len(self.stderr_excerpt)
            + len(self.traceback_excerpt)
            + len(self.git_diff_summary)
            + len(self.validator_rejection_reason)
        )


def build_step_recovery_evidence(
    *,
    failure_envelope: Any,
    debug_feedback_envelope: Any,
    step_record: Any,
    step_output: str,
    task_title: str,
    task_prompt: str,
) -> ExecutionRecoveryEvidence:
    """Assemble a recovery evidence packet from step-level failure data.

    Uses only data already in memory at the execution_loop.py Trigger A point.
    """
    failure_class = str(
        getattr(debug_feedback_envelope, "failure_class", None)
        or getattr(failure_envelope, "root_cause", "unknown")
        or "unknown"
    )
    failed_command = str(
        getattr(debug_feedback_envelope, "failed_command", "")
        or (getattr(failure_envelope, "input", None) or {}).get("verification")
        or ""
    )
    exit_code: Optional[int] = getattr(debug_feedback_envelope, "return_code", None)

    stderr_raw = "\n".join(
        part
        for part in [
            str(getattr(step_record, "error_message", "") or ""),
            str(getattr(step_record, "verification_output", "") or ""),
        ]
        if part
    )
    stderr_excerpt = _truncate(stderr_raw, _MAX_STDERR)
    stdout_excerpt = _truncate(str(step_output or ""), _MAX_STDOUT)
    traceback_excerpt = _truncate(_extract_traceback(stderr_raw), _MAX_TRACEBACK)

    validator_reasons = list(
        getattr(debug_feedback_envelope, "validator_reasons", []) or []
    )[:3]

    return ExecutionRecoveryEvidence(
        task_title=str(task_title or "")[:200],
        task_description=_truncate(str(task_prompt or ""), _MAX_DESCRIPTION),
        failed_command=_truncate(failed_command, _MAX_COMMAND),
        exit_code=exit_code,
        stdout_excerpt=stdout_excerpt,
        stderr_excerpt=stderr_excerpt,
        traceback_excerpt=traceback_excerpt,
        changed_files=list(getattr(step_record, "files_changed", []) or [])[:20],
        git_diff_summary="",
        requested_symbols=[],
        active_human_guidance=[],
        validator_rejection_reason=_truncate(
            "; ".join(validator_reasons), _MAX_VALIDATOR_REASON
        ),
        failure_class=failure_class,
    )


def build_completion_recovery_evidence(
    *,
    completion_validation: Any,
    debug_feedback_envelope: Any,
    orchestration_state: Any,
    task_title: str,
    task_prompt: str,
) -> ExecutionRecoveryEvidence:
    """Assemble a recovery evidence packet from completion-validation failure data.

    Uses only data already in memory at the completion_flow.py Trigger B point.
    """
    failure_class = str(
        getattr(debug_feedback_envelope, "failure_class", None)
        or "completion_validation_failed"
    )
    details = dict(getattr(completion_validation, "details", {}) or {})
    verification_output = str(details.get("verification_output_preview") or "")

    reasons_text = "; ".join(
        list(getattr(completion_validation, "reasons", []) or [])[:5]
    )
    stderr_raw = reasons_text + (
        "\n" + verification_output if verification_output else ""
    )
    stderr_excerpt = _truncate(stderr_raw, _MAX_STDERR)
    traceback_excerpt = _truncate(
        _extract_traceback(verification_output), _MAX_TRACEBACK
    )

    validator_reasons = list(getattr(completion_validation, "reasons", []) or [])[:3]

    sym_check = dict((details.get("symbol_verification") or {}))
    missing_symbols = list(sym_check.get("missing", []) or [])[:10]

    return ExecutionRecoveryEvidence(
        task_title=str(task_title or "")[:200],
        task_description=_truncate(str(task_prompt or ""), _MAX_DESCRIPTION),
        failed_command=str(details.get("verification_command") or "")[:_MAX_COMMAND],
        exit_code=None,
        stdout_excerpt="",
        stderr_excerpt=stderr_excerpt,
        traceback_excerpt=traceback_excerpt,
        changed_files=list(getattr(orchestration_state, "changed_files", []) or [])[
            :20
        ],
        git_diff_summary="",
        requested_symbols=missing_symbols,
        active_human_guidance=[],
        validator_rejection_reason=_truncate(
            "; ".join(validator_reasons), _MAX_VALIDATOR_REASON
        ),
        failure_class=failure_class,
    )
