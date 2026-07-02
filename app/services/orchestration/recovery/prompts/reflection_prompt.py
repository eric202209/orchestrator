"""Phase 17B: Reflection prompt builder.

Produces a concise prompt asking the LLM to diagnose the failure and suggest
a corrective action. The LLM produces an improved recovery artifact only —
it never decides whether to retry.
"""

from __future__ import annotations

from typing import Optional

from app.services.orchestration.recovery.failure_event import FailureEvent

_DETERMINISTIC_CONSTRAINTS = """\
Constraints (non-negotiable):
- Do not suggest changing retry logic, timeouts, or recovery policies.
- Do not modify planning, validation, or orchestration state.
- Focus only on the immediate failure described above.
- Provide a specific, actionable fix — not a generic suggestion.
- If no fix is possible, respond with exactly: NO_RECOVERY_POSSIBLE
"""


def build_reflection_prompt(
    failure_event: FailureEvent,
    previous_attempt: Optional[str] = None,
) -> str:
    """Build a minimal reflection prompt from a FailureEvent.

    Does NOT ask the LLM to decide whether another retry should occur.
    The LLM produces a corrective action artifact only.
    """
    lines = [
        "# Recovery Reflection",
        "",
        "## Failure Summary",
        f"- Failure class: {failure_event.failure_class}",
        f"- Source: {failure_event.source}",
        f"- Orchestration status: {failure_event.orchestration_status or 'unknown'}",
        f"- Exception type: {failure_event.exception_type or 'unknown'}",
        "",
        "## Exception",
        f"{failure_event.error_message[:600]}",
        "",
    ]

    if previous_attempt:
        lines += [
            "## Previous Recovery Attempt",
            f"{str(previous_attempt)[:400]}",
            "",
        ]

    lines += [
        _DETERMINISTIC_CONSTRAINTS,
        "",
        "## Your Task",
        "Diagnose the failure above and provide one specific corrective action.",
        "Be concise. If no fix is possible, respond: NO_RECOVERY_POSSIBLE",
    ]

    return "\n".join(lines)
