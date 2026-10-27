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


class InferenceRoutingPolicy(str, enum.Enum):
    """Provider-neutral policy that owns runtime identity resolution."""

    ROLE_BASED = "role_based"
    SINGLE_MODEL = "single_model"


@dataclass(frozen=True)
class RoleRuntimeConfiguration:
    """Complete, provider-neutral ownership for one runtime invocation."""

    role: BackendRole
    backend_name: str
    model_family: str
    adaptation_profile: str
    routing_policy: InferenceRoutingPolicy = InferenceRoutingPolicy.ROLE_BASED
    provider_endpoint: str | None = None
    fallback_enabled: bool | None = None

    def __post_init__(self) -> None:
        # Accept the historical string constructor form while ensuring every
        # resolved configuration carries the canonical enum value.
        if not isinstance(self.role, BackendRole):
            try:
                object.__setattr__(self, "role", BackendRole(str(self.role)))
            except ValueError as exc:
                raise ValueError(f"Unknown runtime role: {self.role!r}") from exc

        if not isinstance(self.routing_policy, InferenceRoutingPolicy):
            try:
                object.__setattr__(
                    self,
                    "routing_policy",
                    InferenceRoutingPolicy(str(self.routing_policy)),
                )
            except ValueError as exc:
                raise ValueError(
                    f"Unknown inference routing policy: {self.routing_policy!r}"
                ) from exc

        for field_name in ("backend_name", "model_family", "adaptation_profile"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"Runtime configuration {field_name} is required")
            object.__setattr__(self, field_name, value)

        if self.routing_policy is InferenceRoutingPolicy.SINGLE_MODEL:
            endpoint = str(self.provider_endpoint or "").strip().rstrip("/")
            if not endpoint:
                raise ValueError(
                    "Single-model runtime configuration provider_endpoint is required"
                )
            object.__setattr__(self, "provider_endpoint", endpoint)
            if self.fallback_enabled is not False:
                raise ValueError("Single-model routing requires fallback_enabled=False")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["role"] = self.role.value
        payload.pop("routing_policy")
        payload.pop("provider_endpoint")
        payload.pop("fallback_enabled")
        if self.routing_policy is InferenceRoutingPolicy.SINGLE_MODEL:
            payload.update(
                {
                    "routing_policy": self.routing_policy.value,
                    "adapter": self.backend_name,
                    "provider_endpoint": self.provider_endpoint,
                    "fallback_enabled": False,
                }
            )
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
