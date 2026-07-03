"""Phase 17D: optional supplemental reflection evidence for recovery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class ReflectionEvidence:
    """Normalized reflection output carried as advisory recovery context.

    Reflection evidence never selects a strategy, changes budgets, or bypasses
    validators. It is supplemental diagnostic context only.
    """

    summary: str
    suggested_fix: str = ""
    confidence: Optional[str] = None
    source: str = "reflection_retry"

    @classmethod
    def from_reflection_result(
        cls,
        reflection_result: Any,
    ) -> Optional["ReflectionEvidence"]:
        if reflection_result is None:
            return None
        if isinstance(reflection_result, cls):
            return reflection_result

        llm_output = str(getattr(reflection_result, "llm_output", "") or "").strip()
        if not llm_output:
            return None

        return cls(
            summary=llm_output[:1000],
            suggested_fix="",
            confidence=None,
            source=str(
                getattr(reflection_result, "strategy", "") or "reflection_retry"
            ),
        )
