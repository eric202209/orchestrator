"""Phase 17B: Reflection Retry strategy.

Executes exactly one LLM-assisted reflection attempt for unknown/parse failures.
No recursion. No loops. Safe fallback on any error.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.services.orchestration.recovery.failure_event import FailureEvent
from app.services.orchestration.recovery.prompts.reflection_prompt import (
    build_reflection_prompt,
)

logger = logging.getLogger(__name__)

_NO_RECOVERY_SENTINEL = "NO_RECOVERY_POSSIBLE"


@dataclass
class RecoveryResult:
    """Outcome of a recovery strategy execution."""

    success: bool
    failure_class: str
    strategy: str
    outcome: str  # "success" | "failed" | "skipped"
    duration_ms: int
    llm_output: Optional[str] = None
    error: Optional[str] = None


class ReflectionRetryStrategy:
    """Single-shot LLM reflection retry.

    Calls the LLM exactly once with a reflection prompt.
    Returns a RecoveryResult regardless of outcome — never raises.
    """

    @staticmethod
    def execute(
        failure_event: FailureEvent,
        llm_callable: Optional[Callable[[str], str]],
        orchestration_state: Any = None,
        previous_attempt: Optional[str] = None,
    ) -> RecoveryResult:
        """Execute one reflection retry. Returns RecoveryResult, never raises."""
        start_ms = int(time.monotonic() * 1000)

        if llm_callable is None:
            logger.debug(
                "[17B] reflection_retry skipped: no llm_callable (failure_class=%s)",
                failure_event.failure_class,
            )
            return RecoveryResult(
                success=False,
                failure_class=failure_event.failure_class,
                strategy="retry_with_reflection",
                outcome="skipped",
                duration_ms=int(time.monotonic() * 1000) - start_ms,
                error="no_llm_callable",
            )

        try:
            prompt = build_reflection_prompt(
                failure_event, previous_attempt=previous_attempt
            )
            raw_output = llm_callable(prompt)
        except Exception as exc:
            logger.warning("[17B] reflection_retry llm_callable raised: %s", exc)
            return RecoveryResult(
                success=False,
                failure_class=failure_event.failure_class,
                strategy="retry_with_reflection",
                outcome="failed",
                duration_ms=int(time.monotonic() * 1000) - start_ms,
                error=f"llm_callable_raised:{exc}",
            )

        output_str = str(raw_output or "").strip()
        duration_ms = int(time.monotonic() * 1000) - start_ms

        if not output_str or _NO_RECOVERY_SENTINEL in output_str:
            logger.info(
                "[17B] reflection_retry: LLM indicated no recovery possible "
                "(failure_class=%s)",
                failure_event.failure_class,
            )
            return RecoveryResult(
                success=False,
                failure_class=failure_event.failure_class,
                strategy="retry_with_reflection",
                outcome="failed",
                duration_ms=duration_ms,
                llm_output=output_str[:200] if output_str else None,
                error="no_recovery_possible",
            )

        logger.info(
            "[17B] reflection_retry: LLM produced output (%d chars, failure_class=%s)",
            len(output_str),
            failure_event.failure_class,
        )
        return RecoveryResult(
            success=True,
            failure_class=failure_event.failure_class,
            strategy="retry_with_reflection",
            outcome="success",
            duration_ms=duration_ms,
            llm_output=output_str[:1000],
        )
