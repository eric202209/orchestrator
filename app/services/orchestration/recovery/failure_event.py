"""Phase 17A: Canonical FailureEvent — normalizes every runtime failure into one schema."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Optional


@dataclass
class FailureEvent:
    """Canonical in-memory representation of a runtime failure.

    Constructed by FailureClassifier; consumed by RecoveryStrategyRegistry.
    Not persisted to the database — emitted as part of audit event details.
    """

    event_id: str
    source: str  # "terminal_reason" | "failure_class" | "execution" | "planning" | "unknown"
    failure_class: str  # canonical string; "unknown_failure" when unclassifiable
    error_message: str  # str(exc)[:400]
    created_at: str  # ISO8601

    session_id: Optional[int] = None
    task_id: Optional[int] = None
    step_index: Optional[int] = None
    terminal_reason: Optional[str] = None
    exception_type: Optional[str] = None
    orchestration_phase: Optional[str] = None
    orchestration_status: Optional[str] = None
    signature_hash: Optional[str] = None


def make_failure_event(
    *,
    failure_class: str,
    source: str,
    error_message: str,
    session_id: Optional[int] = None,
    task_id: Optional[int] = None,
    step_index: Optional[int] = None,
    terminal_reason: Optional[str] = None,
    exception_type: Optional[str] = None,
    orchestration_phase: Optional[str] = None,
    orchestration_status: Optional[str] = None,
    signature_hash: Optional[str] = None,
) -> FailureEvent:
    """Construct a FailureEvent with a generated event_id and timestamp."""
    return FailureEvent(
        event_id=uuid.uuid4().hex[:12],
        source=source,
        failure_class=failure_class,
        error_message=error_message[:400],
        created_at=datetime.now(UTC).isoformat(),
        session_id=session_id,
        task_id=task_id,
        step_index=step_index,
        terminal_reason=terminal_reason,
        exception_type=exception_type,
        orchestration_phase=orchestration_phase,
        orchestration_status=orchestration_status,
        signature_hash=signature_hash,
    )
