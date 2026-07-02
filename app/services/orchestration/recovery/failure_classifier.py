"""Phase 17A: Deterministic failure classifier.

Maps runtime exceptions and orchestration state to a canonical FailureEvent.
Classification is rule-based — no AI involved.

Rules are evaluated in precedence order; the first match wins.
Unknown failures fall through to "unknown_failure" without changing runtime behaviour.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.services.orchestration.recovery.failure_event import (
    FailureEvent,
    make_failure_event,
)

logger = logging.getLogger(__name__)

# Markers used to detect timeout-flavoured exceptions (mirrors FailureCoordinator).
_TIMEOUT_MARKERS = ("time limit", "timeout", "timed out")


def _is_timeout(exc: Exception) -> bool:
    exc_str = str(exc).lower()
    return any(m in exc_str for m in _TIMEOUT_MARKERS)


def _orchestration_status(orchestration_state: Any) -> Optional[str]:
    status = getattr(orchestration_state, "status", None)
    if status is None:
        return None
    return str(status.value) if hasattr(status, "value") else str(status)


def _orchestration_phase(orchestration_state: Any) -> Optional[str]:
    phase = getattr(orchestration_state, "current_phase", None)
    if phase is None:
        return None
    return str(phase.value) if hasattr(phase, "value") else str(phase)


class FailureClassifier:
    """Deterministic rule-based failure classifier."""

    @staticmethod
    def classify(
        exc: Exception,
        orchestration_state: Any = None,
        *,
        session_id: Optional[int] = None,
        task_id: Optional[int] = None,
        step_index: Optional[int] = None,
    ) -> FailureEvent:
        """Classify a runtime exception into a canonical FailureEvent.

        Rules in precedence order — first match wins:
        1. wrapper_timeout_noise     — timeout exc + orchestration already DONE
        2. project_mutation_lock_conflict — ProjectMutationLockError
        3. bounded_debug_repair_timeout   — bounded debug repair timeout
        4. planning_lock_contention       — planning lock wait timeout
        5. execution_timeout / planning_timeout — other timeouts
        6. debug_parse_error              — parse-related exceptions
        7. unknown_failure                — fallback
        """
        runtime_diagnostics = getattr(exc, "runtime_diagnostics", None) or {}
        exc_str = str(exc)
        exc_type = type(exc).__name__
        orch_status = _orchestration_status(orchestration_state)
        orch_phase = _orchestration_phase(orchestration_state)

        common = dict(
            session_id=session_id,
            task_id=task_id,
            step_index=step_index,
            exception_type=exc_type,
            orchestration_phase=orch_phase,
            orchestration_status=orch_status,
            error_message=exc_str,
        )

        # Rule 1: wrapper_timeout_noise
        # A timeout that fires after the orchestration has already reached DONE.
        # Validated maintenance finding — treat as annotation, not task failure.
        if _is_timeout(exc) and orch_status == "done":
            return make_failure_event(
                failure_class="wrapper_timeout_noise",
                source="execution",
                **common,
            )

        # Rule 2: project_mutation_lock_conflict
        try:
            from app.services.workspace.project_mutation_lock import (
                ProjectMutationLockError,
            )

            if isinstance(exc, ProjectMutationLockError):
                return make_failure_event(
                    failure_class="project_mutation_lock_conflict",
                    source="execution",
                    **common,
                )
        except ImportError:
            pass

        # Rule 3: bounded_debug_repair_timeout
        try:
            from app.services.orchestration.phases.failure_flow import (
                _is_bounded_debug_repair_timeout,
            )

            if _is_bounded_debug_repair_timeout(exc, runtime_diagnostics):
                return make_failure_event(
                    failure_class="bounded_debug_repair_timeout",
                    source="execution",
                    **common,
                )
        except ImportError:
            pass

        # Rule 4: planning_lock_contention
        if (
            runtime_diagnostics.get("timeout_boundary") == "planning_lock_wait"
            or "openclaw planning lock" in exc_str.lower()
        ):
            return make_failure_event(
                failure_class="planning_lock_contention",
                source="planning",
                **common,
            )

        # Rule 5: execution_timeout / planning_timeout (generic timeouts)
        if _is_timeout(exc):
            if orch_phase and "plan" in orch_phase:
                failure_class = "planning_timeout"
                source = "planning"
            else:
                failure_class = "execution_timeout"
                source = "execution"
            return make_failure_event(
                failure_class=failure_class,
                source=source,
                **common,
            )

        # Rule 6: debug_parse_error
        if "parse" in exc_str.lower():
            return make_failure_event(
                failure_class="debug_parse_error",
                source="execution",
                **common,
            )

        # Rule 7: unknown_failure (fallback)
        return make_failure_event(
            failure_class="unknown_failure",
            source="unknown",
            **common,
        )
