"""Backend registry for orchestration model/runtime integrations.

This module creates a stable seam between orchestration policy and the
underlying execution runtime. OpenClaw remains the only implemented backend
today, but the registry is shaped so additional providers can be added without
rewiring planning/execution flows.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class BackendCapabilities:
    """Declared backend capabilities used for routing and operator visibility."""

    supports_planning: bool
    supports_step_execution: bool
    supports_debug_repair: bool
    supports_streaming: bool
    supports_checkpoint_resume: bool
    supports_tool_execution: bool
    supports_json_mode: bool
    max_context_tokens: Optional[int] = None
    reliability_tier: str = "standard"
    latency_tier: str = "standard"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BackendDescriptor:
    """Backend metadata exposed to orchestration and operator surfaces."""

    name: str
    display_name: str
    implementation: str
    default_model_family: str
    available: bool
    capabilities: BackendCapabilities

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["capabilities"] = self.capabilities.to_dict()
        return payload


def _local_openclaw_descriptor() -> BackendDescriptor:
    return BackendDescriptor(
        name="local_openclaw",
        display_name="Local OpenClaw",
        implementation="app.services.agents.openclaw_service.OpenClawSessionService",
        default_model_family="local",
        available=True,
        capabilities=BackendCapabilities(
            supports_planning=True,
            supports_step_execution=True,
            supports_debug_repair=True,
            supports_streaming=True,
            supports_checkpoint_resume=True,
            supports_tool_execution=True,
            supports_json_mode=False,
            max_context_tokens=128000,
            reliability_tier="standard",
            latency_tier="local",
        ),
    )


_BACKEND_REGISTRY = {
    descriptor.name: descriptor
    for descriptor in [
        _local_openclaw_descriptor(),
    ]
}


def list_supported_backends() -> List[BackendDescriptor]:
    """Return the currently registered orchestration backends."""

    return list(_BACKEND_REGISTRY.values())


def get_backend_descriptor(name: Optional[str]) -> BackendDescriptor:
    """Resolve a configured backend name to a concrete descriptor."""

    normalized = (name or "local_openclaw").strip().lower()
    return _BACKEND_REGISTRY.get(normalized, _BACKEND_REGISTRY["local_openclaw"])
