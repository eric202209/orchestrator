"""Provider-neutral runtime configuration passed to backend adapters."""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass
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


# Compatibility alias retained while internal and external callers migrate to
# the role-explicit name.
RuntimeConfiguration = RoleRuntimeConfiguration
