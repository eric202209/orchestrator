"""Provider-neutral runtime configuration passed to backend adapters."""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, fields
from typing import Any


class BackendRole(str, enum.Enum):
    """Explicit runtime roles that own a model invocation."""

    PLANNING = "planning"
    EXECUTION = "execution"
    DEBUG_REPAIR = "debug_repair"
    REPAIR = "repair"
    COMPLETION_REPAIR = "completion_repair"


@dataclass(frozen=True)
class RoleRuntimeConfiguration:
    """Complete, provider-neutral ownership for one runtime invocation."""

    role: BackendRole
    backend_name: str
    model_family: str
    adaptation_profile: str

    def __post_init__(self) -> None:
        # Accept the historical string constructor form while ensuring every
        # resolved configuration carries the canonical enum value.
        if not isinstance(self.role, BackendRole):
            try:
                object.__setattr__(self, "role", BackendRole(str(self.role)))
            except ValueError as exc:
                raise ValueError(f"Unknown runtime role: {self.role!r}") from exc

        for field_name in ("backend_name", "model_family", "adaptation_profile"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"Runtime configuration {field_name} is required")
            object.__setattr__(self, field_name, value)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["role"] = self.role.value
        return payload

    def behavioral_identity(self) -> tuple[Any, ...]:
        """Return the role-independent behavior that controls runtime reuse.

        The role itself is intentionally excluded: Planning and Execution are
        different owners but may safely share one runtime only when every
        behavior-affecting configuration dimension is equal. Deriving this
        tuple from dataclass fields means a later configuration dimension is
        automatically included in reuse decisions.
        """

        return tuple(
            getattr(self, field.name) for field in fields(self) if field.name != "role"
        )

    def is_behaviorally_equivalent(self, other: "RoleRuntimeConfiguration") -> bool:
        """Whether two role-owned configurations can share a runtime."""

        if not isinstance(other, RoleRuntimeConfiguration):
            return False
        return self.behavioral_identity() == other.behavioral_identity()


# Compatibility alias retained while internal and external callers migrate to
# the role-explicit name.
RuntimeConfiguration = RoleRuntimeConfiguration
