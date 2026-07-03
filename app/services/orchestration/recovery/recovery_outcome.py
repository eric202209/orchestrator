"""Phase 17C-3: canonical orchestration outcome for active recovery."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.services.orchestration.recovery.recovery_context import RecoveryContext


@dataclass(frozen=True)
class RecoveryOutcome:
    """Immutable registry-level recovery result.

    ``strategy_result`` is the unchanged payload returned by the underlying
    implementation. Dict-like access is preserved for existing execution
    call sites that read ``.get("status")`` and related fields.
    """

    succeeded: bool
    resumed_execution: bool
    strategy_name: str
    duration_ms: int
    failure_class: str
    recovery_context: RecoveryContext
    audit_event_ids: tuple[str, ...] = field(default_factory=tuple)
    strategy_result: Mapping[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.strategy_result.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.strategy_result[key]

    def __contains__(self, key: object) -> bool:
        return key in self.strategy_result

    def __iter__(self) -> Iterator[str]:
        return iter(self.strategy_result)

    def items(self):
        return self.strategy_result.items()

    def keys(self):
        return self.strategy_result.keys()

    def values(self):
        return self.strategy_result.values()

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            return dict(self.strategy_result) == dict(other)
        return super().__eq__(other)
