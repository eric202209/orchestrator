"""Typed terminal-attempt handoff between Planning and the worker boundary.

Phase 36 Maintenance LA1-ER4.  Planning owns attempt evidence; the
FailureCoordinator owns Session lifecycle/recovery policy.  A terminal
Planning result therefore has to travel from the Planning-result dispatch to
the existing outer worker failure boundary instead of returning directly.

This module carries immutable facts only.  It deliberately contains no
recovery policy, no retry policy, no Session terminality policy, and no
continuation policy: those remain owned by the FailureCoordinator and the
canonical lifecycle transitions.
"""

from __future__ import annotations

from typing import Any


class TerminalAttemptHandoffError(RuntimeError):
    """A durably recorded terminal Planning attempt awaiting reconciliation.

    Raising this means: the attempt failure evidence is committed and the
    Session lifecycle reconciliation is still outstanding.  It does **not**
    mean the Session is already terminal.
    """

    def __init__(
        self,
        *,
        failure_category: str,
        failure_reason: str,
        session_id: int | None = None,
        task_execution_id: int | None = None,
        expected_session_instance_id: str | None = None,
        terminal_failure: bool = True,
    ) -> None:
        super().__init__(failure_reason)
        self.failure_category = failure_category
        self.failure_reason = failure_reason
        self.terminal_failure = terminal_failure
        self.session_id = session_id
        self.task_execution_id = task_execution_id
        # Consumed by the canonical lifecycle expected-identity CAS.
        self.expected_session_instance_id = expected_session_instance_id
        self.expected_task_execution_id = task_execution_id

    def to_facts(self) -> dict[str, Any]:
        return {
            "failure_category": self.failure_category,
            "failure_reason": self.failure_reason,
            "terminal_failure": self.terminal_failure,
            "session_id": self.session_id,
            "task_execution_id": self.task_execution_id,
            "expected_session_instance_id": self.expected_session_instance_id,
        }


def terminal_attempt_handoff_from_planning_result(
    result: dict[str, Any],
    *,
    session_id: int | None,
    task_execution_id: int | None,
    expected_session_instance_id: str | None,
) -> TerminalAttemptHandoffError:
    """Normalize a terminal Planning result into the typed handoff.

    Fact-only: the original ``failure_category`` and reason are preserved so
    the outer boundary and the FailureCoordinator see the same terminal
    failure the Planning phase produced.
    """

    return TerminalAttemptHandoffError(
        failure_category=str(result.get("failure_category") or "planning_failed"),
        failure_reason=str(result.get("reason") or "planning_failed"),
        session_id=session_id,
        task_execution_id=task_execution_id,
        expected_session_instance_id=expected_session_instance_id,
        terminal_failure=bool(result.get("terminal_failure")),
    )
