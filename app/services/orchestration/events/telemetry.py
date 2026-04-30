"""Structured orchestration phase telemetry helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional


def record_phase_event(
    orchestration_state: Any,
    *,
    phase: str,
    status: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Persist a compact phase event into orchestration state."""

    payload = {
        "timestamp": datetime.utcnow().isoformat(),
        "phase": phase,
        "status": status,
        "message": message,
        "details": details or {},
    }
    if orchestration_state is not None:
        phase_history = getattr(orchestration_state, "phase_history", None)
        if isinstance(phase_history, list):
            phase_history.append(payload)
    return payload


def emit_phase_event(
    orchestration_state: Any,
    emit_live: Any,
    *,
    level: str,
    phase: str,
    message: str,
    status: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Record a phase event and mirror it to live logs."""

    payload = record_phase_event(
        orchestration_state,
        phase=phase,
        status=(status or level).lower(),
        message=message,
        details=details,
    )
    metadata = {"phase": phase}
    if details:
        metadata.update(details)
    emit_live(level, message, metadata=metadata)
    return payload
