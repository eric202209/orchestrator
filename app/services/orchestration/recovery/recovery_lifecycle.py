"""Phase 17C-3: canonical audit lifecycle for active recovery."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.recovery.recovery_context import RecoveryContext

logger = logging.getLogger(__name__)


@dataclass
class RecoveryLifecycle:
    """Emit registry-owned recovery lifecycle events.

    This helper coordinates audit state only. It does not decide whether a
    strategy can recover, mutate recovery context, or execute recovery logic.
    """

    context: RecoveryContext
    strategy_name: str = "execution_recovery"
    _event_ids: list[str] = field(default_factory=list)
    _started_at: float = field(default_factory=time.perf_counter)

    @property
    def audit_event_ids(self) -> tuple[str, ...]:
        return tuple(self._event_ids)

    def duration_ms(self) -> int:
        return max(0, int((time.perf_counter() - self._started_at) * 1000))

    def started(self) -> None:
        self._emit(
            EventType.RECOVERY_STARTED,
            {
                **self._base_details(),
                "lifecycle_phase": "started",
            },
        )

    def completed(self, *, result: dict[str, Any]) -> None:
        self._emit(
            EventType.RECOVERY_COMPLETED,
            {
                **self._base_details(),
                "lifecycle_phase": "completed",
                "status": result.get("status"),
                "duration_ms": self.duration_ms(),
            },
        )

    def resumed(self, *, result: dict[str, Any]) -> None:
        self._emit(
            EventType.RECOVERY_RESUMED,
            {
                **self._base_details(),
                "lifecycle_phase": "resumed",
                "status": result.get("status"),
                "duration_ms": self.duration_ms(),
            },
        )

    def failed(
        self, *, result: Optional[dict[str, Any]] = None, error: str = ""
    ) -> None:
        details = {
            **self._base_details(),
            "lifecycle_phase": "failed",
            "duration_ms": self.duration_ms(),
        }
        if result is not None:
            details["status"] = result.get("status")
            details["reason"] = result.get("reason")
        if error:
            details["error"] = error[:400]
        self._emit(EventType.RECOVERY_FAILED, details)

    def _base_details(self) -> dict[str, Any]:
        return {
            "failure_class": getattr(self.context.evidence, "failure_class", None),
            "strategy": self.strategy_name,
            "scope": self.context.scope,
            "step_index": self.context.step_index,
            "session_id": self.context.session_id,
            "task_id": self.context.task_id,
        }

    def _emit(self, event_type: str, details: dict[str, Any]) -> None:
        if (
            self.context.project_dir is None
            or self.context.session_id is None
            or self.context.task_id is None
        ):
            return
        try:
            from app.services.orchestration.state.persistence import (
                append_orchestration_event,
            )

            event = append_orchestration_event(
                project_dir=self.context.project_dir,
                session_id=self.context.session_id,
                task_id=self.context.task_id,
                event_type=event_type,
                details=details,
                parent_event_id=self.context.parent_event_id,
            )
            event_id = event.get("event_id")
            if event_id:
                self._event_ids.append(str(event_id))
        except Exception as exc:
            logger.debug(
                "[17C-3] lifecycle event emit failed (%s): %s", event_type, exc
            )
